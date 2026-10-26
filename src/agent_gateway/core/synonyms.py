"""编程同义词映射表：把中文搜索词扩展成对应的英文编程术语。

内置默认（`SYNONYM_MAP`）+ 用户增量（`config.yaml` 的 `mcp.synonyms`，经 env
`MCP_SYNONYMS` 注入）。合并语义与项目里其它「增量补充」配置一致——**同名覆盖、异名追加**：
用户要加几个词，不必抄一份完整内置表回来维护。

**不迁入 notes**：检索质量不该成为可被静默改坏的变量——改写坏了的表现是「搜不到」，
没有任何报错，与「这条记录本来就不存在」无法区分。

被 `server_search.expand_query()` 引用，用于 FTS5 全文搜索的同义词扩展。
"""

from __future__ import annotations

import json
import os

from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("synonyms")

#: 用户自定义同义词表的 env 名（跨语言契约，Swift 侧 `MCPProcessManager.synonymsEnvKey`
#: 同值，由 `scripts/check-router-config-contract.py` 交叉断言）。
SYNONYMS_ENV_KEY = "MCP_SYNONYMS"

SYNONYM_MAP: dict[str, str] = {
    "请求": "request fetch http api network",
    "网络": "network http request fetch api",
    "日期": "date time calendar",
    "时间": "time date calendar",
    "格式": "format parse stringify",
    "解析": "parse format stringify",
    "排序": "sort filter order",
    "过滤": "filter sort order",
    "缓存": "cache store persist",
    "存储": "store cache persist database",
    "认证": "auth login token",
    "登录": "auth login token",
    "错误": "error exception fault",
    "异常": "error exception fault",
    "配置": "config setting option",
    "翻译": "translate translation",
    "复制": "copy paste clipboard",
    "粘贴": "paste copy clipboard",
    "剪贴板": "clipboard paste copy",
    "搜索": "search query find",
    "查询": "search query find",
    "导出": "export",
    "导入": "import",
    "创建": "create new make",
    "删除": "delete remove",
    "更新": "update modify change",
    "读取": "read load fetch get",
    "写入": "write save store set",
    "文件": "file path",
    "路径": "path directory file",
    "连接": "connect link session connection",
    "生成": "generate create produce build",
    "转换": "convert transform change to",
    "计算": "compute calculate count math",
    "合并": "merge combine join union concat",
    "分隔": "split separate divide tokenize",
    "验证": "validate check verify assert ensure",
    "处理": "process handle deal with",
    "发送": "send emit dispatch post push",
    "接收": "receive accept listen on poll",
    "注册": "register subscribe add hook listen",
    "选择": "select choose pick option",
    "状态": "state status stage phase mode flag",
    "工具": "tool utility helper util function",
    "数据": "data info information value",
    "文本": "text string content message",
    "数字": "number integer float numeric count",
    "映射": "map dict dictionary key value",
    "回调": "callback handler delegate closure",
    "通知": "notify inform alert broadcast emit",
    "打开": "open start enable launch activate",
    "关闭": "close stop disable shutdown deactivate",
    "渲染": "render display show draw paint",
    "遍历": "loop iterate cycle traversal each",
    "异步": "async await promise future defer",
    "同步": "sync await blocking sequential",
    "初始化": "init setup bootstrap configure",
    "清除": "clear reset clean purge flush",
    "注入": "inject dependency di wire",
    "捕获": "catch try error exception throw",
}

#: 合并结果缓存：env 在进程启动时固定，故首次解析后复用
#: （配置改了要重启 MCP 服务才生效，与其它注入型配置一致）。
_merged: dict[str, str] | None = None


def load_synonyms() -> dict[str, str]:
    """内置默认 + 用户增量（同名覆盖、异名追加）。首次调用解析并缓存。

    返回的表**只读**——调用方（`expand_query`）只做查询，不要就地修改它。
    """
    global _merged
    if _merged is not None:
        return _merged

    merged = dict(SYNONYM_MAP)
    raw = os.environ.get(SYNONYMS_ENV_KEY, "").strip()
    if raw:
        try:
            user = json.loads(raw)
        except ValueError as e:
            # 坏了不能静默：表现是「我配的同义词没生效」，而这与「这个词本来没配」无法区分
            logger.error(f"{SYNONYMS_ENV_KEY} 不是合法 JSON，已忽略用户同义词: {e}")
            user = None
        if isinstance(user, dict):
            merged.update({str(k): str(v) for k, v in user.items()})
        elif user is not None:
            logger.error(
                f"{SYNONYMS_ENV_KEY} 不是对象（得到 {type(user).__name__}），已忽略"
            )

    _merged = merged
    return _merged
