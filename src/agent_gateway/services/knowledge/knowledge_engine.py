#!/usr/bin/env python3
"""知识面板的引擎：展开概念 → 检索依据 → 生成卡片。"""

import html as htmlmod
import re
import time
import urllib.parse

from agent_gateway.llm import client as llm_client
from agent_gateway.core import prompt_loader
from agent_gateway.core.l10n import t
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.search.web_search import fetch, is_engine_page, wiki
from agent_gateway.services.search.web_search import search as web_search_query

logger = setup_logging("knowledge_refill")

# 预算常量（调优值，不进配置面）。一次逻辑调用内置「解析失败降级重试一次」，
# 故一轮 HTTP 次数上界 = MAX_LLM_CALLS_PER_RUN * 2。
# 单张卡走完 expand + card 要 2 次，一轮最多补 3 张，保证未读池能跟上阅读速度。
MAX_LLM_CALLS_PER_RUN = 10
CARDS_PER_TOPIC_PER_RUN = 3

# 依据充分性判据
MIN_EVIDENCE_CHARS = 800
MIN_WIKI_CHARS = 600
MIN_SEARCH_HITS = 3
MIN_PAGE_HITS = 2
MAX_EVIDENCE_CHARS = 12000
EVIDENCE_WALL_CLOCK = (
    90.0  # gather_evidence 的整体墙钟预算（5 家引擎轮流超时会拖到 100s+）
)
PAGE_FETCH_LIMIT = 2  # 抓几页正文（每页 15s 超时，与上面的墙钟预算相配）
PAGE_TEXT_LIMIT = 5000  # 单页保留字数

# 结果码
OK = "ok"
EXHAUSTED = "exhausted"
EXPAND_FAIL = "expand_fail"
BASIS_SHORT = "basis_insufficient"
CARD_FAIL = "card_fail"
# 上游不可用（LLM 返回空 / 断连 / 超时）。**必须与内容层面的失败分开**：调用方不能把它
# 计入主题的 fail_streak —— 那是一次网络抖动就永久烧掉一个主题（实测踩到：调用失败直接
# 把两个主题标成 done）。内容失败（CARD_FAIL）仍然计入。
CARD_UNAVAILABLE = "card_unavailable"
# 选题闸门拒绝：概念越出 scope 或属于产品/趋势词。**不计 fail_streak**——
# 这是闸门在正常工作，不是管线失败；概念落 rejected 后不再被重问。
SCOPE_REJECTED = "scope_rejected"
# 闸门调用本身失败（上游不可用），与 CARD_UNAVAILABLE 同语义。
GATE_UNAVAILABLE = "gate_unavailable"

# 概念在「失败」时落库的 status（防止下一轮把同一个概念重问一遍）。
# OK/EXHAUSTED 由调用方处理（前者写 done、后者不写 concept 行）。
CONCEPT_STATUS = {BASIS_SHORT: "skipped", CARD_FAIL: "failed"}


def _res(
    code: str,
    *,
    concept: str = "",
    why_now: str = "",
    body: str = "",
    evidence: str = "",
    urls=None,
    calls: int = 0,
) -> dict:
    return {
        "code": code,
        "concept": concept,
        "why_now": why_now,
        "body": body,
        "evidence": evidence,
        "urls": list(urls or []),
        "calls": calls,
    }


def _llm_json(
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None = None,
    disable_thinking: bool = False,
    feature: str | None = None,
    allow_partial: bool = True,
) -> dict | None:
    """Ask the model for JSON; retry once with thinking disabled on failure.

    `allow_partial=False` is for body output: a card recovered from a truncated
    payload is a half card and must be dropped.
    """
    for attempt in range(2):
        failure = ""
        try:
            raw = llm_client.chat(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                disable_thinking=(disable_thinking or attempt > 0),
                label=feature or "knowledge",
                raise_on_error=True,
            )
        except llm_client.LLMClientError as e:
            # A truncated response still carries usable leading fields
            # (expand's concept, gate's accept).
            raw = e.content if e.kind == "truncated" else None
            failure = f"{e.kind}/finish_reason={e.finish_reason}"
            if raw:
                logger.warning(
                    "%s: output truncated (finish_reason=%s), recovering fields",
                    feature,
                    e.finish_reason,
                )
            else:
                logger.warning("%s: call failed kind=%s (%s)", feature, e.kind, e)
        if raw:
            parsed, repaired = llm_client.parse_json_partial(raw)
            if parsed and (allow_partial or not repaired):
                if feature and prompt_loader.missing_json_keys(feature, parsed):
                    logger.warning(
                        "%s: model output is missing template-declared keys", feature
                    )
                return parsed
            if repaired:
                logger.warning("%s: partial output discarded", feature)
                failure = f"partial-x2/attempt={attempt + 1}"
            else:
                logger.warning(
                    "%s: unparseable JSON (attempt %d): %r",
                    feature,
                    attempt + 1,
                    raw[:200],
                )
                failure = f"unparseable-json/attempt={attempt + 1}"
        else:
            logger.warning("%s: empty response (attempt %d)", feature, attempt + 1)
            failure = failure or f"empty/attempt={attempt + 1}"
        if failure:
            last = failure
        if attempt == 0:
            logger.warning("%s: retrying once with thinking disabled", feature)
    tgt = llm_client.default_target()
    host = urllib.parse.urlparse(tgt.endpoint).netloc or "-"
    logger.error(
        "%s: no parseable JSON after two attempts "
        "(model=%s host=%s max_tokens=%s prompt_chars=%d last=%s)",
        feature,
        tgt.model,
        host,
        max_tokens,
        len(prompt),
        last,
    )
    return None


def _strip_html(raw: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmlmod.unescape(h)).strip()


def _usable_source(url: str) -> bool:
    """Reject non-http and search-engine result pages before they become sources."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return not is_engine_page(url)


def _page_text(url: str, concept: str) -> str:
    """抓页面正文，并以 concept 出现处为中心截取——整页里大半是导航与推荐位。"""
    try:
        text = _strip_html(fetch(url, "bing", timeout=15))
    except Exception:
        return ""
    idx = text.lower().find(concept.lower())
    start = max(0, idx - 1500) if idx >= 0 else 0
    return text[start : start + PAGE_TEXT_LIMIT]


def gather_evidence(concept: str, theme: str) -> dict | None:
    """检索依据；充分则返回 {text, urls}，不足返回 None。

    只用 `web_search`（维基 API + 多引擎级联 + 抓正文）——它内部已把限流/网络异常
    吞成 None/[]，故这里不再套 try/except，只保证整体不超墙钟预算。

    **必须抓正文**：只有标题与摘要时证据通常不足 300 字，而这正是卡片质量的控制旋钮；
    中文技术术语在维基上多半没有条目（`wiki()` 对「中间表示」也返回 None），
    摘要那点文本撑不起一张卡片，模型只能靠编。
    """
    deadline = time.monotonic() + EVIDENCE_WALL_CLOCK
    sources: list[dict] = []
    parts: list[str] = []

    hit = wiki(concept)
    wiki_text = (hit or {}).get("extract", "").strip()
    has_wiki = len(wiki_text) >= MIN_WIKI_CHARS
    if has_wiki:
        parts.append(wiki_text[:6000])
        if _usable_source(hit.get("url", "")):
            sources.append(
                {"url": hit["url"], "title": (hit.get("title") or "").strip()}
            )

    hits: list[dict] = []
    if time.monotonic() < deadline:
        # 只查 concept，**不拼 theme**：web_search 的多词判据是 AND（每个词都必须出现在
        # 标题或摘要里），拼上主题会让「分阶段架构 编译原理」这类查询直接返回 0 条。
        hits = web_search_query(concept, min_results=5) or []
        # concept 是模型自己组织的说法时（实测「分层协议栈模型」只搜到 1 条无效结果，
        # 而标准名称「TCP/IP 四层模型」有 10 条），退化为主题级检索 —— 概念本就从主题里
        # 长出来，主题的权威资料本就涵盖它。否则大量正常主题会因命名问题一张卡都出不来。
        if len(hits) < MIN_SEARCH_HITS and theme:
            seen = {h.get("url") for h in hits}
            for h in web_search_query(theme, min_results=5) or []:
                if h.get("url") not in seen:
                    hits.append(h)

    fetched = 0
    for r in hits[:6]:
        if time.monotonic() >= deadline:
            break
        url = r.get("url", "")
        if not _usable_source(url):
            url = ""
        body = ""
        if url and fetched < PAGE_FETCH_LIMIT:
            body = _page_text(url, concept)
            if body:
                fetched += 1
        block = body or "\n".join(
            x
            for x in (
                r.get("title", ""),
                r.get("date", ""),
                (r.get("snippet") or "")[:300],
            )
            if x
        )
        if block.strip():
            parts.append(block)
        if url:
            sources.append({"url": url, "title": (r.get("title") or "").strip()})

    text = "\n\n---\n".join(parts)
    # 依据不足是**正常结果**（概念名常是模型自己组织的说法,搜不到是常态）,但它是
    # `basis_insufficient` 的唯一成因 —— 不打出实际数字,调 `MIN_*` 阈值就只能靠猜。
    if len(text) < MIN_EVIDENCE_CHARS:
        logger.info(
            "insufficient evidence (%s): text %d chars < %d · wiki=%s · search %d hits · pages %d",
            concept,
            len(text),
            MIN_EVIDENCE_CHARS,
            has_wiki,
            len(hits),
            fetched,
        )
        return None
    # 要么有一条像样的维基条目,要么至少抓到 2 页正文
    if not has_wiki and fetched < MIN_PAGE_HITS:
        logger.info(
            "insufficient evidence (%s): text %d chars but no wiki entry and only %d pages (<%d)",
            concept,
            len(text),
            fetched,
            MIN_PAGE_HITS,
        )
        return None
    # 「概念词必须出现在证据里」这条判据**故意不加**：概念名常是模型自己组织的说法
    # （「分层协议栈模型」对「TCP/IP 四层模型」），而证据来自主题级检索时必然不含它。
    # 相关性由检索侧保证 —— search 内部有 _matches 过滤，_wiki_title_for 内部有重叠校验。
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS] + t("mcp.knowledge.evidence_truncated")

    seen, uniq = set(), []
    for item in sources:
        url = item["url"]
        if url and url not in seen and _usable_source(url):
            seen.add(url)
            uniq.append(item)
    return {"text": text, "urls": uniq}


def advance(
    theme: str,
    done_text: str,
    *,
    scope: str = "",
    exclude: str = "",
    disliked: str = "",
) -> dict:
    """跑完四步：展开概念 → 选题闸门 → 检索依据 → 生成卡片。

    `scope` / `exclude` 是主题的硬边界，同时喂给 expand、gate、card 三个提示词；
    `disliked` 是用户踩过的方向，作为负反馈喂给 expand 与 gate。

    **幻觉自检已移除**：它是输入最大的一步(卡片全文 + ≤12000 字证据),上游对长输入处理
    约 25 秒后直接断连(`RemoteProtocolError: peer closed connection`),几乎必挂;而缩短
    证据又会把被截掉那半段的真实内容判成「卡片自行添加」(实测过)。留一个必然失败的步骤,
    只会把已经生成好的卡片全部丢掉。约束卡片不编造的职责改由 knowledge_card 提示词承担。
    """
    calls = 0

    p = prompt_loader.prompt(
        "knowledge_expand",
        theme=theme,
        done=done_text or t("mcp.knowledge.no_content"),
        scope=scope,
        exclude=exclude,
        disliked=disliked,
    )
    d = _llm_json(p, max_tokens=768, disable_thinking=True, feature="knowledge_expand")
    calls += 1
    if d is None:
        return _res(EXPAND_FAIL, calls=calls)
    if d.get("exhausted") is True:
        return _res(EXHAUSTED, calls=calls)
    concept = (d.get("concept") or "").strip()
    why_now = (d.get("why_now") or "").strip()
    if not concept:
        # 未标 exhausted 却给不出概念 = 契约违约
        logger.warning("expand neither marked exhausted nor returned a concept: %r", d)
        return _res(EXPAND_FAIL, calls=calls)

    # 独立一次判断，而不是让 expand 自评：生成与审核分开，才不会被「自然引出」的
    # 叙事自我说服。低 token + 零温度，单次成本可忽略。
    gate = _llm_json(
        prompt_loader.prompt(
            "knowledge_gate",
            theme=theme,
            scope=scope,
            exclude=exclude,
            concept=concept,
            why_now=why_now,
            disliked=disliked,
        ),
        max_tokens=512,
        temperature=0,
        disable_thinking=True,
        feature="knowledge_gate",
    )
    calls += 1
    if gate is None:
        return _res(GATE_UNAVAILABLE, concept=concept, why_now=why_now, calls=calls)
    if gate.get("accept") is not True:
        logger.info(
            "scope rejected: %s / %s (%s)", theme, concept, gate.get("reason", "")
        )
        return _res(SCOPE_REJECTED, concept=concept, why_now=why_now, calls=calls)

    ev = gather_evidence(concept, theme)
    if ev is None:
        return _res(BASIS_SHORT, concept=concept, why_now=why_now, calls=calls)

    # Thinking off: an 8k completion took 60-180s in practice, and upstream
    # ReadTimeout / disconnects were the main failure source. The prompt caps the
    # body at 1500 words, so 6144 tokens leaves headroom; a truncated card is retried.
    p = prompt_loader.prompt(
        "knowledge_card",
        theme=theme,
        concept=concept,
        evidence=ev["text"],
        scope=scope,
        exclude=exclude,
    )
    d = _llm_json(
        p,
        max_tokens=6144,
        temperature=0.7,
        disable_thinking=True,
        feature="knowledge_card",
        allow_partial=False,
    )
    calls += 1
    if d is None:
        return _res(CARD_UNAVAILABLE, concept=concept, why_now=why_now, calls=calls)
    body = (d.get("card") or "").strip()
    if not body:
        return _res(CARD_FAIL, concept=concept, why_now=why_now, calls=calls)

    return _res(
        OK,
        concept=concept,
        why_now=why_now,
        body=body,
        evidence=ev["text"],
        urls=ev["urls"],
        calls=calls,
    )
