"""Shared language tables for the graph indexer.

Kept free of parser imports so MCP tools can reuse the extension map without
pulling in tree-sitter grammars.
"""

from __future__ import annotations

#: Bumping either forces a full rebuild on both sides.
ENGINE_VERSION = "2"
SCHEMA_VERSION = "1"

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

#: No project file can define these; they are reported as external, not missing.
EXTERNAL_TYPE_NAMES = frozenset(
    {
        "Any",
        "AnyObject",
        "Array",
        "Bool",
        "Callable",
        "CaseIterable",
        "CGPoint",
        "CGRect",
        "CGSize",
        "CGFloat",
        "Character",
        "Codable",
        "Data",
        "Date",
        "Dict",
        "Dictionary",
        "Double",
        "Equatable",
        "Error",
        "Float",
        "Hashable",
        "Identifiable",
        "IndexPath",
        "Int",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Iterable",
        "Iterator",
        "List",
        "Mapping",
        "Never",
        "NSError",
        "NSLayoutConstraint",
        "NSObject",
        "NSString",
        "NSView",
        "NSViewController",
        "NSWindow",
        "Optional",
        "Path",
        "Result",
        "Sendable",
        "Sequence",
        "Set",
        "String",
        "Tuple",
        "Type",
        "URL",
        "UUID",
        "Union",
        "UInt",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "Void",
    }
)

EXTERNAL_CALL_NAMES = frozenset(
    {
        "assert",
        "assertEqual",
        "assertFalse",
        "assertIn",
        "assertRaises",
        "assertTrue",
        "bool",
        "debugPrint",
        "dict",
        "enumerate",
        "fail",
        "fatalError",
        "filter",
        "float",
        "getattr",
        "hasattr",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "open",
        "precondition",
        "print",
        "range",
        "set",
        "setattr",
        "sorted",
        "str",
        "tuple",
        "zip",
    }
)
