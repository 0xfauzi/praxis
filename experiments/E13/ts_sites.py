"""E13 TypeScript and JavaScript site oracle: the compiler-API route of
docs/design/04-languages.md section 4.2 at site grain, as a node script held in a string
so that praxis's own repository holds no JavaScript file (a JavaScript file here would
make praxis's own X1 a two-language scan, a different extractor join for its truth).

``ts_accept.py`` writes ``SCRIPT`` to a temporary file and runs
``node <file> <typescript.js> <repo_root>`` with the repository-relative file list on
stdin; the script prints one JSON object.
"""

SCRIPT = r"""
// E13 TypeScript and JavaScript site oracle: the compiler-API route of
// docs/design/04-languages.md section 4.2, at site grain.
//
// Usage: node ts_sites.js <typescript.js> <repo_root> < file-list > out.json
// The file list is repository-relative paths, one per line. For each file the
// TypeScript parser's own tree gives the import sites (ImportDeclaration,
// ExportDeclaration with a module specifier, ImportEqualsDeclaration with an
// ExternalModuleReference, a CallExpression on `import` or on the identifier
// `require` with a string-literal-like argument, ImportTypeNode with a string
// literal, and sourceFile.referencedFiles), and ts.resolveModuleName resolves each
// with the options and mode the compiler would use. The project of a file is the
// nearest tsconfig.json whose parsed fileNames hold it, else the first project it
// references, transitively, that does (tsserver's rule); a file with no
// tsconfig.json above it gets the generated config E13 gives a plain JavaScript
// repository. Targets pass through realpath, so workspace links land on repository
// files. Nothing here writes to the repository.
const ts = require(process.argv[2]);
const path = require("path");
const fs = require("fs");

const root = fs.realpathSync(path.resolve(process.argv[3]));
const files = fs.readFileSync(0, "utf8").split("\n").filter(Boolean);
const parsedCache = new Map();
const nearestCache = new Map();
let configErrors = 0;

const GENERATED = ts.convertCompilerOptionsFromJson({
  allowJs: true, checkJs: false, module: "nodenext", moduleResolution: "nodenext",
  maxNodeModuleJsDepth: 0, skipLibCheck: true, types: [],
}, root).options;

function nearest(dir) {
  if (nearestCache.has(dir)) return nearestCache.get(dir);
  let found = null;
  const own = path.join(dir, "tsconfig.json");
  if (fs.existsSync(own) && fs.statSync(own).isFile()) found = own;
  else if (dir !== root && dir.startsWith(root)) found = nearest(path.dirname(dir));
  nearestCache.set(dir, found);
  return found;
}

function parsed(cfg) {
  if (parsedCache.has(cfg)) return parsedCache.get(cfg);
  const errors = [];
  const host = { ...ts.sys, onUnRecoverableConfigFileDiagnostic: (d) => errors.push(d) };
  const existing = { allowJs: true, ignoreDeprecations: "6.0" };
  const p = ts.getParsedCommandLineOfConfigFile(cfg, existing, host);
  const out = {
    cfg, options: p ? p.options : GENERATED,
    names: new Set(p ? p.fileNames.map((f) => path.resolve(f)) : []),
    references: p && p.projectReferences
      ? p.projectReferences.map((r) => path.resolve(ts.resolveProjectReferencePath(r)))
      : [],
  };
  configErrors += errors.length + (p ? p.errors.filter((e) => e.category === 1).length : 1);
  parsedCache.set(cfg, out);
  return out;
}

function project(abs) {
  const near = nearest(path.dirname(abs));
  if (!near) return { cfg: null, options: GENERATED };
  const first = parsed(near);
  if (first.names.has(abs)) return first;
  const queue = [...first.references];
  const seen = new Set([near]);
  while (queue.length) {
    const ref = queue.shift();
    if (seen.has(ref) || !fs.existsSync(ref)) continue;
    seen.add(ref);
    const p = parsed(ref);
    if (p.names.has(abs)) return p;
    queue.push(...p.references);
  }
  return first;
}

const FN = new Set([ts.SyntaxKind.FunctionDeclaration, ts.SyntaxKind.FunctionExpression,
  ts.SyntaxKind.ArrowFunction, ts.SyntaxKind.MethodDeclaration, ts.SyntaxKind.Constructor,
  ts.SyntaxKind.GetAccessor, ts.SyntaxKind.SetAccessor]);

function inFunction(node) {
  for (let p = node.parent; p; p = p.parent) if (FN.has(p.kind)) return true;
  return false;
}

function where(target) {
  if (!target) return { target: null, outside: false };
  let real;
  try { real = fs.realpathSync(target); } catch (e) { real = path.resolve(target); }
  const rel = path.relative(root, real);
  const outside = rel.startsWith("..") || path.isAbsolute(rel)
    || rel.split(path.sep).includes("node_modules");
  return { target: outside ? null : rel.split(path.sep).join("/"), outside };
}

function scriptKind(rel) {
  if (/\.(tsx|jsx)$/.test(rel)) return ts.ScriptKind.TSX;
  if (/\.(js|mjs|cjs)$/.test(rel)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

function isDefer(clause) {
  return !!(clause && ts.SyntaxKind.DeferKeyword !== undefined
    && clause.phaseModifier === ts.SyntaxKind.DeferKeyword);
}

function readFile(rel) {
  const abs = path.join(root, rel);
  const text = fs.readFileSync(abs, "utf8");
  const { cfg, options } = project(abs);
  const format = ts.getImpliedNodeFormatForFile(abs, undefined, ts.sys, options);
  const sf = ts.createSourceFile(abs, text,
    { languageVersion: ts.ScriptTarget.Latest, impliedNodeFormat: format }, true, scriptKind(rel));
  const sites = [];
  const add = (node, spec, kind, deferred, typeOnly) => {
    let mode;
    try { mode = ts.getModeForUsageLocation(sf, spec, options); } catch (e) { mode = undefined; }
    const r = ts.resolveModuleName(spec.text, abs, options, ts.sys, undefined, undefined, mode);
    const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    const resolved = r.resolvedModule ? r.resolvedModule.resolvedFileName : null;
    sites.push({ line, specifier: spec.text, kind, deferred, type_only: typeOnly,
      ...where(resolved) });
  };
  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteralLike(node.moduleSpecifier)) {
      const c = node.importClause;
      const kind = !c ? "side_effect" : c.isTypeOnly ? "import_type" : "import";
      add(node, node.moduleSpecifier, kind, isDefer(c), !!(c && c.isTypeOnly));
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier
        && ts.isStringLiteralLike(node.moduleSpecifier)) {
      add(node, node.moduleSpecifier, node.isTypeOnly ? "export_type_from" : "export_from",
        false, node.isTypeOnly);
    } else if (ts.isImportEqualsDeclaration(node)
        && ts.isExternalModuleReference(node.moduleReference)
        && ts.isStringLiteralLike(node.moduleReference.expression)) {
      add(node, node.moduleReference.expression, "import_equals", false, node.isTypeOnly);
    } else if (ts.isCallExpression(node) && node.arguments.length >= 1
        && ts.isStringLiteralLike(node.arguments[0])) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
        add(node, node.arguments[0], "dynamic_import", true, false);
      } else if (ts.isIdentifier(node.expression) && node.expression.text === "require"
          && node.arguments.length === 1) {
        add(node, node.arguments[0], "require", inFunction(node), false);
      }
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)
        && ts.isStringLiteral(node.argument.literal)) {
      add(node, node.argument.literal, "import_type_node", true, true);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  for (const ref of sf.referencedFiles) {
    const target = path.resolve(path.dirname(abs), ref.fileName);
    const found = fs.existsSync(target) ? target : null;
    sites.push({ line: sf.getLineAndCharacterOfPosition(ref.pos).line + 1,
      specifier: ref.fileName, kind: "reference_path", deferred: false, type_only: true,
      ...where(found) });
  }
  return { cfg: cfg && path.relative(root, cfg), parse_errors: sf.parseDiagnostics.length,
    sites };
}

const result = {};
const t0 = process.hrtime.bigint();
for (const rel of files) result[rel] = readFile(rel);
const ms = Number(process.hrtime.bigint() - t0) / 1e6;
process.stdout.write(JSON.stringify({
  typescript: ts.version, node: process.version, files: result, total_ms: ms,
  configs: [...parsedCache.keys()].map((c) => path.relative(root, c)),
  config_errors: configErrors,
}));
"""
