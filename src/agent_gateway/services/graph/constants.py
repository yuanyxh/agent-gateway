"""Shared language tables for the graph indexer.

Kept free of parser imports so MCP tools can reuse the extension map without
pulling in tree-sitter grammars.
"""

from __future__ import annotations

#: Extension to parser language. Mirrors the host's GraphProtocol mapping.
EXTENSIONS = {
    ".py": "python",
    ".swift": "swift",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".vue": "vue",
    ".m": "objc",
    ".h": "objc",
}

JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue")

LANG_FAMILY = {
    ".py": "py",
    ".swift": "apple",
    ".m": "apple",
    ".h": "apple",
    ".js": "web",
    ".jsx": "web",
    ".mjs": "web",
    ".cjs": "web",
    ".ts": "web",
    ".tsx": "web",
    ".vue": "web",
    ".java": "jvm",
    ".kt": "jvm",
    ".kts": "jvm",
}

TYPE_KINDS = frozenset(
    {
        "class",
        "struct",
        "enum",
        "actor",
        "extension",
        "protocol",
        "interface",
        "component",
        "object",
        "record",
    }
)

CALLABLE_KINDS = frozenset(
    {"function", "method", "class", "struct", "enum", "actor", "initializer"}
)
