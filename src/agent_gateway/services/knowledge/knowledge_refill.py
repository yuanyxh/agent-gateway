#!/usr/bin/env python3
"""知识面板的补货入口：把卡片补进池子。

由 Swift 的 `KnowledgeService` 按间隔起子进程调用（**不是 TaskScheduler** ——
它的 handleMissed 以「今天第一个 cron 点」为基准，任何 sub-daily 的 cron 都会
静默退化成每天一次）。池子够了就直接退出，所以调用方可以放心按固定节拍唤醒。

退出码恒为 0，结果以 JSON 打印到 stdout；真正的问题写 `GATEWAY_CACHE/knowledge_refill.log`。
不走 TaskScheduler 的通知路径 —— 每 30 分钟弹一条系统通知会疯掉。
"""

import json
import sys

from agent_gateway.core import config
from agent_gateway.services.knowledge import knowledge_engine as engine
from agent_gateway.services.knowledge import knowledge_pool as pool
from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("knowledge_refill")


def _refill_topics(budget: int) -> tuple[int, int, list]:
    """主题化：沿主线按序推进，只补从未展示的卡。"""
    results = []
    added = 0
    max_streak = config.knowledge_max_fail_streak()
    target = config.knowledge_topic_target()
    while added < engine.CARDS_PER_TOPIC_PER_RUN and budget >= 3:
        if pool.unseen_count(pool.KIND_TOPIC) >= target:
            break
        topic = pool.next_topic()
        if topic is None:
            logger.warning("no active topic, skipping topic refill")
            break
        done = "\n".join(
            f"{c['seq'] + 1}. {c['name']}" for c in pool.concepts_of(topic["id"])
        )
        disliked = "\n".join(f"- {t}" for t in pool.disliked_titles(topic["id"]))
        r = engine.advance(
            topic["name"],
            done,
            scope=topic.get("scope", ""),
            exclude=topic.get("exclude", ""),
            disliked=disliked,
        )
        budget -= r["calls"]
        code = r["code"]

        if code == engine.OK:
            pool.commit_card(
                kind=pool.KIND_TOPIC,
                theme=topic["name"],
                title=r["concept"],
                body=r["body"],
                topic_kind=topic["kind"],
                evidence=r["evidence"],
                evidence_urls=r["urls"],
                topic_id=topic["id"],
                why_now=r["why_now"],
            )
            pool.reset_topic_fail(topic["id"])
            added += 1
            logger.info("topic card added: %s / %s", topic["name"], r["concept"])
        elif code == engine.EXHAUSTED:
            pool.mark_topic_done(topic["id"])
            logger.info("topic exhausted, marked done: %s", topic["name"])
        elif code in (
            engine.EXPAND_FAIL,
            engine.CARD_UNAVAILABLE,
            engine.GATE_UNAVAILABLE,
        ):
            # 这几种都是**上游不可用**(LLM 返回空/断连/超时,llm_client.log 可证),不是内容问题:
            # 既不计入 fail_streak、也不在本轮继续硬撞 —— 否则一次网络抖动就能把主题永久标成
            # done(实测踩到:调用失败直接把两个主题烧成 done)。
            logger.warning(
                "upstream unavailable, stopping topic refill: %s (%s)",
                topic["name"],
                code,
            )
            break
        elif code == engine.SCOPE_REJECTED:
            # 闸门拒绝是正常结果：落 rejected 防止下轮重问，不烧 fail_streak，
            # 继续在同一主题里找下一个候选（预算每轮有限，不会无限重试）。
            if r["concept"]:
                pool.insert_concept(
                    topic["id"],
                    r["concept"],
                    r["why_now"],
                    pool.next_concept_seq(topic["id"]),
                    "rejected",
                )
            logger.info(
                "scope rejected, trying next: %s / %s", topic["name"], r["concept"]
            )
        else:
            # 概念已消费(哪怕卡片没生成)也要落一行,否则下一轮会把同一个概念重问一遍,
            # 而 expand 拿到的 {done} 会原地打转。落库的 status 区分失败原因。
            status = engine.CONCEPT_STATUS.get(code)
            if status and r["concept"]:
                pool.insert_concept(
                    topic["id"],
                    r["concept"],
                    r["why_now"],
                    pool.next_concept_seq(topic["id"]),
                    status,
                )
            streak = pool.bump_topic_fail(topic["id"], max_streak)
            logger.warning(
                "topic produced nothing: %s code=%s fail_streak=%d",
                topic["name"],
                code,
                streak,
            )
        results.append(
            {
                "kind": "topic",
                "theme": topic["name"],
                "code": code,
                "concept": r["concept"],
                "calls": r["calls"],
            }
        )
    return budget, added, results


def refill() -> dict:
    """Internal entry used by the CLI shim and external callers."""
    path = pool.init_db()
    added_topics = pool.seed_from_config()
    pruned = pool.prune_expired(config.knowledge_retention_days())
    logger.info(
        "refill start: db=%s new_topics=%d pruned=%d",
        path,
        added_topics,
        pruned,
    )

    budget = engine.MAX_LLM_CALLS_PER_RUN
    budget, made, results = _refill_topics(budget)

    outcomes: dict[str, int] = {}
    for item in results:
        outcomes[item["code"]] = outcomes.get(item["code"], 0) + 1
    summary = {
        "made": made,
        "pruned": pruned,
        "calls_used": engine.MAX_LLM_CALLS_PER_RUN - budget,
        "unseen_topic": pool.unseen_count(pool.KIND_TOPIC),
        "pool_topic": pool.pool_count(pool.KIND_TOPIC),
        "outcomes": outcomes,
        "results": results,
    }
    logger.info(
        "refill done: made=%d calls_used=%d outcomes=%s",
        made,
        summary["calls_used"],
        json.dumps(outcomes, ensure_ascii=False, sort_keys=True),
    )
    return summary


def main() -> int:
    print(json.dumps(refill(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
