#!/usr/bin/env node
/**
 * JS/TS/JSX/TSX AST symbol extractor using @babel/parser.
 *
 * Usage: cat file.ts | node parse.mjs tsx
 *   argv[2] = effective extension (js|ts|jsx|tsx)
 *   stdin  = file content
 *   stdout = JSON: {symbols: [...]}
 *
 * Extracts at module level:
 *   - exported functions/classes/variables (named + default)
 *   - internal function/class/variable declarations
 *   - interface / type alias declarations
 *   - re-exports (export { X } from "...")
 */

import * as babel from "@babel/parser";

function symbol(name, kind, extra = {}, loc = null) {
  if (loc?.start) {
    extra = { ...extra, start_line: loc.start.line, end_line: loc.end.line };
  }
  return { name, kind, ...extra };
}

function extractName(decl) {
  if (!decl) return null;
  if (decl.id) return decl.id.name;
  // For VariableDeclarator with destructuring
  if (decl.type === "VariableDeclarator" && decl.id.type === "Identifier") {
    return decl.id.name;
  }
  return null;
}

function buildSignature(node, kind) {
  if (kind === "function") {
    const params = (node.params || [])
      .map((p) => {
        if (p.type === "Identifier") return p.name;
        if (p.type === "AssignmentPattern" && p.left.type === "Identifier") {
          return p.left.name + "?";
        }
        if (p.type === "RestElement") return "..." + (p.argument?.name || "");
        return "param";
      })
      .join(", ");
    let sig = (node.id ? node.id.name : "(anonymous)") + "(" + params + ")";
    if (node.returnType) {
      sig += ": " + simplifyTypeAnnotation(node.returnType);
    } else if (node.typeAnnotation) {
      sig += ": " + simplifyTypeAnnotation(node.typeAnnotation);
    }
    return sig;
  }
  if (kind === "class") {
    let sig = node.id ? node.id.name : "(anonymous)";
    if (node.superClass) {
      sig += " extends " + (node.superClass.name || "(expr)");
    }
    if (node.implements && node.implements.length) {
      sig +=
        " implements " +
        node.implements
          .map((i) => i.name || i.expression?.name || "")
          .filter(Boolean)
          .join(", ");
    }
    return sig;
  }
  return null;
}

function builtinTypeToStr(t) {
  if (!t) return "";
  const m = t.match(/^TS(Number|String|Boolean|Symbol|Null|Undefined|Void|Never|Any|Unknown|Object|BigInt)Keyword$/);
  return m ? m[1].toLowerCase() : "";
}

function typeAnnotationToStr(anno) {
  if (!anno) return "";
  if (typeof anno === "string") return anno;
  const t = anno.typeAnnotation || anno;
  switch (t.type) {
    case "TSStringKeyword": return "string";
    case "TSNumberKeyword": return "number";
    case "TSBooleanKeyword": return "boolean";
    case "TSSymbolKeyword": return "symbol";
    case "TSNullKeyword": return "null";
    case "TSUndefinedKeyword": return "undefined";
    case "TSVoidKeyword": return "void";
    case "TSNeverKeyword": return "never";
    case "TSAnyKeyword": return "any";
    case "TSUnknownKeyword": return "unknown";
    case "TSBigIntKeyword": return "bigint";
    case "TSObjectKeyword": return "object";
    case "TSArrayType":
      return typeAnnotationToStr(t.elementType) + "[]";
    case "TSTypeReference":
      if (t.typeName) {
        const base = t.typeName.name || t.typeName.typeName?.name || "";
        return base;
      }
      return "";
    case "TSTypeLiteral":
      return "{ ... }";
    case "TSUnionType":
      return t.types.map((tt) => typeAnnotationToStr(tt)).join(" | ");
    case "TSIntersectionType":
      return t.types.map((tt) => typeAnnotationToStr(tt)).join(" & ");
    case "TSFunctionType":
      const params = (t.parameters || [])
        .map((p) => p.name + ": " + typeAnnotationToStr(p.typeAnnotation))
        .join(", ");
      const ret = t.typeAnnotation ? typeAnnotationToStr(t.typeAnnotation) : "void";
      return "(" + params + ") => " + ret;
    case "TSOptionalType":
      return typeAnnotationToStr(t.typeAnnotation) + "?";
    case "TSRestType":
      return "..." + typeAnnotationToStr(t.typeAnnotation);
    case "TSTupleType":
      return "[" + t.elementTypes.map((e) => typeAnnotationToStr(e)).join(", ") + "]";
    default:
      return "";
  }
}

function simplifyTypeAnnotation(anno) {
  if (!anno) return "";
  const s = typeAnnotationToStr(anno);
  if (s && s.length < 80) return s;
  return "";
}

function getDocComment(leadingComments) {
  if (!leadingComments || leadingComments.length === 0) return null;
  const docs = leadingComments
    .filter((c) => c.type === "CommentBlock" || c.type === "CommentLine")
    .map((c) => c.value.trim())
    .join("\n");
  return docs.length > 0 ? docs : null;
}

/**
 * Walk AST node recursively and collect symbols.
 */
function extractSymbols(node, _state = {}) {
  const symbols = [];
  const state = _state;
  const C = (n) => extractSymbols(n, state).forEach((s) => symbols.push(s));

  if (!node || typeof node !== "object") return symbols;

  // Process based on node type
  switch (node.type) {
    // ── Declarations ──
    case "FunctionDeclaration":
    case "ClassDeclaration": {
      const name = node.id ? node.id.name : "(anonymous)";
      const kind = node.type === "FunctionDeclaration" ? "function" : "class";
      const sig = buildSignature(node, kind);
      symbols.push(
        symbol(name, kind, {
          signature: sig,
          visibility: state.exportScope ? "export" : null,
          doc_comment: getDocComment(node.leadingComments || node.innerComments),
        }, node.loc)
      );
      // For class, also extract methods
      if (node.body?.body) {
        for (const member of node.body.body) {
          C(member);
        }
      }
      break;
    }

    case "VariableDeclaration": {
      const visibility = state.exportScope ? "export" : null;
      for (const decl of node.declarations) {
        const name = extractName(decl);
        if (!name) continue;
        const init = decl.init;
        let kind = "variable";
        let sig = null;
        if (init && (init.type === "FunctionExpression" || init.type === "ArrowFunctionExpression")) {
          kind = "function";
          const anonName = node.kind === "const" && name ? name : "(anonymous)";
          const params = (init.params || [])
            .map((p) => (p.type === "Identifier" ? p.name : "param"))
            .join(", ");
          sig = anonName + "(" + params + ")";
          // Get return type if available
          if (init.returnType) {
            sig += ": ...";
          }
        } else if (init && init.type === "ClassExpression") {
          kind = "class";
          sig = name;
          if (init.superClass) {
            sig += " extends " + (init.superClass.name || "(expr)");
          }
        } else if (decl.id.typeAnnotation) {
          sig =
            name +
            ": " +
            simplifyTypeAnnotation(decl.id.typeAnnotation);
        }
        symbols.push(
          symbol(name, kind, {
            signature: sig,
            visibility,
            doc_comment: getDocComment(node.leadingComments || node.innerComments),
          }, decl.loc)
        );
      }
      break;
    }

    case "TSInterfaceDeclaration":
    case "TSTypeAliasDeclaration": {
      const kind = node.type === "TSInterfaceDeclaration" ? "interface" : "type";
      const name = node.id.name;
      let sig = name;
      // Type parameters
      if (node.typeParameters) {
        const params = (node.typeParameters.params || [])
          .map((tp) => tp.name)
          .join(", ");
        sig += "<" + params + ">";
      }
      // Extends / implements for interface
      if (node.extends && node.extends.length) {
        sig +=
          " extends " +
          node.extends
            .map((e) => {
              if (e.type === "TSExpressionWithTypeArguments")
                return e.expression.name || "";
              return e.name || "";
            })
            .filter(Boolean)
            .join(", ");
      }
      symbols.push(
        symbol(name, kind, {
          signature: sig,
          visibility: state.exportScope ? "export" : null,
          doc_comment: getDocComment(node.leadingComments || node.innerComments),
        }, node.loc)
      );
      break;
    }

    case "TSEnumDeclaration": {
      const name = node.id.name;
      symbols.push(
        symbol(name, "enum", {
          visibility: state.exportScope ? "export" : null,
          doc_comment: getDocComment(node.leadingComments || node.innerComments),
        }, node.loc)
      );
      break;
    }

    case "TSModuleDeclaration": {
      // namespace/module
      if (node.id) {
        const name = node.id.name || "(namespace)";
        symbols.push(
          symbol(name, "namespace", {
            visibility: state.exportScope ? "export" : null,
          }, node.loc)
        );
      }
      // Walk body
      if (node.body) C(node.body);
      break;
    }

    // ── Exports ──
    case "ExportNamedDeclaration": {
      if (node.declaration) {
        // export function/class/const/interface ...
        state.exportScope = true;
        C(node.declaration);
        state.exportScope = false;
      }
      if (node.specifiers) {
        for (const spec of node.specifiers) {
          // export { X } or export { X as Y }
          const localName = spec.local?.name || spec.exported?.name || "";
          symbols.push(
            symbol(localName, "export", {
              visibility: "export",
              signature: spec.exported?.name
                ? localName + " as " + spec.exported.name
                : localName,
            }, node.loc)
          );
        }
      }
      break;
    }

    case "ExportDefaultDeclaration": {
      const decl = node.declaration;
      if (decl.id) {
        symbols.push(
          symbol(decl.id.name, decl.type === "FunctionDeclaration" ? "function" : "class", {
            visibility: "default",
            signature: buildSignature(decl, decl.type === "FunctionDeclaration" ? "function" : "class"),
            doc_comment: getDocComment(decl.leadingComments || decl.innerComments),
          }, decl.loc)
        );
      } else if (decl.type === "FunctionDeclaration" || decl.type === "ClassExpression") {
        // Anonymous default
        const kind = decl.type === "FunctionDeclaration" ? "function" : "class";
        symbols.push(
          symbol("default", kind, {
            visibility: "default",
            signature: buildSignature(decl, kind),
          }, decl.loc)
        );
      } else if (decl.type === "AssignmentExpression" && decl.left?.name) {
        // export default name = value
      }
      break;
    }

    case "ExportAllDeclaration": {
      // export * from "..."
      symbols.push(
        symbol(node.exported?.name || "*", "re-export", {
          visibility: "export",
          signature: "from " + (node.source?.value || ""),
        }, node.loc)
      );
      break;
    }

    // ── Class members ──
    case "ClassMethod":
    case "ClassPrivateMethod":
    case "ObjectMethod": {
      const name = node.key?.name || node.key?.value || "(computed)";
      const kind = node.kind === "get" ? "getter" : node.kind === "set" ? "setter" : "method";
      const params = (node.params || [])
        .map((p) => (p.type === "Identifier" ? p.name : "param"))
        .join(", ");
      const sig = name + "(" + params + ")";
      symbols.push(
        symbol(name, kind, {
          signature: sig,
          visibility: state.exportScope ? "export" : null,
          doc_comment: getDocComment(node.leadingComments),
        }, node.loc)
      );
      break;
    }

    case "ClassProperty":
    case "ClassPrivateProperty": {
      const name = node.key?.name || node.key?.value || "(computed)";
      let sig = name;
      if (node.typeAnnotation) {
        sig += ": " + simplifyTypeAnnotation(node.typeAnnotation);
      }
      symbols.push(
        symbol(name, "property", {
          signature: sig,
          visibility: null,
        }, node.loc)
      );
      break;
    }

    // ── Recursive walk children ──
    default:
      // Walk all enumerable properties for AST nodes
      for (const key of Object.keys(node)) {
        if (key === "leadingComments" || key === "trailingComments" || key === "innerComments") continue;
        const val = node[key];
        if (Array.isArray(val)) {
          for (const item of val) {
            if (item && typeof item.type === "string") C(item);
          }
        } else if (val && typeof val.type === "string") {
          C(val);
        }
      }
  }

  return symbols;
}

/**
 * Walk the AST and collect file-level import specifiers.
 */
function extractImports(node, imports = []) {
  if (!node || typeof node !== "object") return imports;

  const push = (raw, kind, loc) => {
    if (typeof raw === "string" && raw.length > 0) {
      imports.push({ raw_target: raw, kind, line: loc?.start?.line ?? null });
    }
  };

  switch (node.type) {
    case "ImportDeclaration":
      push(node.source?.value, "static", node.loc);
      break;
    case "ExportNamedDeclaration":
    case "ExportAllDeclaration":
      if (node.source?.value) push(node.source.value, "export_from", node.loc);
      break;
    case "TSImportEqualsDeclaration":
      if (node.moduleReference?.type === "TSExternalModuleReference") {
        push(node.moduleReference.expression?.value, "static", node.loc);
      }
      break;
    case "CallExpression": {
      const callee = node.callee;
      const arg = node.arguments?.[0];
      if (
        callee?.type === "Identifier" &&
        callee.name === "require" &&
        arg &&
        typeof arg.value === "string"
      ) {
        push(arg.value, "require", node.loc);
      } else if (callee?.type === "Import" && arg && typeof arg.value === "string") {
        push(arg.value, "dynamic", node.loc);
      }
      break;
    }
  }

  for (const key of Object.keys(node)) {
    if (key === "leadingComments" || key === "trailingComments" || key === "innerComments") continue;
    const val = node[key];
    if (Array.isArray(val)) {
      for (const item of val) {
        if (item && typeof item.type === "string") extractImports(item, imports);
      }
    } else if (val && typeof val.type === "string") {
      extractImports(val, imports);
    }
  }
  return imports;
}

function dedupeImports(imports) {
  const seen = new Set();
  return imports.filter((imp) => {
    const key = `${imp.raw_target}\u0000${imp.kind}\u0000${imp.line ?? 0}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// ── Main ──

const ext = process.argv[2] || "js";

const plugins = [];

// Always include these
plugins.push("decorators-legacy");
plugins.push("importAttributes");
plugins.push("optionalChaining");
plugins.push("nullishCoalescingOperator");

if (ext === "ts" || ext === "tsx") {
  plugins.push("typescript");
}
// Always enable jsx for .js files too: React Native / Expo projects commonly
// use JSX in .js files. errorRecovery:true ensures plain JS files aren't affected.
if (ext === "js" || ext === "jsx" || ext === "tsx") {
  plugins.push("jsx");
}

let content = "";
process.stdin.setEncoding("utf-8");
process.stdin.on("data", (chunk) => (content += chunk));
process.stdin.on("end", () => {
  try {
    const ast = babel.parse(content, {
      sourceType: "unambiguous",
      plugins,
      errorRecovery: true,
      allowImportExportEverywhere: false,
    });

    const symbols = extractSymbols(ast);
    const imports = dedupeImports(extractImports(ast)).slice(0, 2000);
    console.log(JSON.stringify({ symbols, imports }));
  } catch (err) {
    // babel parse error
    console.log(JSON.stringify({ error: err.message }));
  }
});
