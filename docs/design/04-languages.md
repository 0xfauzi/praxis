# 04: ground truth beyond Python

Status: DESIGN, written 2026-10-19 at commit 90cca33, before any E13 extractor data. (The
brief named ab4ad37; between the two only `docs/map/`, `map/`, `systemap.toml` and
`docs/preregistration.md` changed, and no file under `src/`.) It designs the one layer that
keeps praxis Python-only: the static extractor that turns a repository into component
edges (X1, `docs/design/03-measurement.md` section 3.1). The component map, the probes,
the scoring and the recorder use paths and component ids and are language-agnostic
already. Section 2.3 of design 03 says "an extractor for another language is added only
after its own experiment"; this document is that experiment's design. It changes no code,
and nothing it proposes lands before the gate in section 8 passes.

## Sources

Every number below carries one of these tags, or says "needs measuring" and what settles
it. Design choices are labelled [D] and are never measurements.

- [MH] Measured here, on this container, 2026-10-19: 4 CPUs; praxis's venv Python 3.12.13
  unless a line says otherwise; node v22.22.0; tsc 6.0.2 (the TypeScript package at
  `/opt/node22/lib/node_modules/typescript`); go1.24.7; cargo and rustc 1.97.0; javac and
  jdeps 21.0.12.1; rust-analyzer 0.3.3089-standalone, fetched from its GitHub release into
  the session's scratch directory because the rustup component is not installed
  (`rust-analyzer --version` prints "Unknown binary 'rust-analyzer' in official
  toolchain"). Public repositories were cloned over HTTPS, `--depth 1`, into the scratch
  directory. The prototype scripts that produced the TypeScript, Go and Rust numbers ran
  from that scratch directory and are not committed, because this change may add only
  this document. They are smoke measurements that E13 must reproduce, never results.
- [SM] systemap source, read and not depended on: `typescript_imports.py`,
  `typescript_resolve.py`, `typescript_roots.py`, `typescript_config.py`,
  `typescript_reexports.py`, `typescript_package_imports.py`, `typescript_projects.py`,
  `typescript_syntax.py`, `repo_files.py`, `python_imports.py`, and their tests
  (`test_typescript_resolution.py`, `test_typescript_projects.py`,
  `test_trial_typescript_imports.py`, `test_typescript_exports.py`).
- [TD] A tool's own help or output, read here (`jdeps --help`, `rustc -Z help`).
- [E13] `experiments/E13/oracle.py` as it stood at 22:40 on 2026-10-19, being written by
  another agent at the same time as this document.
- [03] `docs/design/03-measurement.md`; [E04] `docs/experiments/E04.md`.

## 0. The task, rewritten

- Altitude. The outcome is not "languages supported". It is that the owner's
  repositories get structural items (P1) and edge history at Stop whatever language the
  agent writes, with truth known to be right. Today a repository that is mostly TypeScript
  gets no D1 or D2 item at all, because every component without Python files is `unknown`
  [03 2.3].
- Depth. The field's instrument for "is this import graph right" is the language's own
  resolver. A careful engineer would not trust a hand lexer until it agreed with tsc,
  `go list`, rustc and javac on repositories it was not tuned on, and would keep
  extraction errors (a missed statement) apart from resolution errors (a wrong file). So
  the oracle is built first, it is the outside judge, and the acceptance rule is written
  here before any extractor data.
- Range. Compilers (module resolution algorithms), lexing (regular expression against
  division, JSX against comparison), build systems (workspaces, manifests, nested
  modules), measurement (held-out repositories, agreement with intervals) and a red team
  (section 7).
- Belief. Precision and recall of at least 0.98 per repository on five held-out
  repositories per language, at a per-file cost no worse than X1 on Python. An ugly
  end-to-end proof for TypeScript was built here first (section 4.8): 2,175 of 2,175
  import sites and 1,269 of 1,269 file edges agree with the TypeScript compiler on three
  public repositories, after one resolver defect the oracle found on first contact.

## 1. The construct, per language

### 1.1 Definition

A file edge f -> g exists when code in file f names code in file g through the language's
module system: a statement or expression whose meaning, as the language's own resolver
reads it, is a file or package of the repository. Edges are lifted to components through
the frozen map exactly as X1 lifts them today [03 3.1], one site per statement.

What an edge is not, in any language: a call graph, a type-inference result or a runtime
dependency. A method call on a value whose type is declared in another file is not an
edge, as it is not in Python X1 today, which counts `Import` and `ImportFrom` only.
Measured [MH]: on sharkdp/fd (commit dbfe46b), rust-analyzer's LSIF dump connects 85
distinct file pairs by some resolved reference, and 5 of them (5.9 percent) only through
method or field references. Those 5 are outside the construct, and the Rust oracle filters
them (section 4.2).

### 1.2 What counts

| Language | Sites that are edges | Target | Not an edge |
|---|---|---|---|
| TypeScript, JavaScript | `import ... from "s"`; `import "s"`; `import type ... from "s"`; `export ... from "s"`; `export type ... from "s"`; `import x = require("s")`; `require("s")` with one string-literal argument; `import("s")` with a string literal; `import("s")` in a type position; `/// <reference path="s" />` | the file the TypeScript resolver picks: tsconfig `paths` and `baseUrl`, extension substitution, index files, `package.json` `exports` and `imports` of workspace packages | a specifier that resolves outside the repository (an npm package); `/// <reference types>` and `lib`; CSS and asset imports (no code); imports the compiler adds with no site (measured [MH]: on sindresorhus/ky, 31 of 125 `--traceResolution` blocks are `react/jsx-runtime`, implied by `"jsx": "react-jsx"`) |
| Go | each import spec whose path is the module path or below it (`go.mod`), or a path in another module of the repository reached through `go.work` or a `replace` to a local directory | the package directory: Go imports packages, never files | the standard library and third-party paths; `import "C"` (cgo); references between files of one package, which need no import |
| Rust | `use` trees; qualified paths anywhere (`crate::a::b`, `super::x`, `self::x`, or `a::b` where `a` is a module or crate in scope); `mod name;`; `extern crate` (2015 edition); `pub use`; path dependencies between workspace crates | the file of the deepest module the path names; a crate root is its `lib.rs` or `main.rs` | method and field references; items reached only through trait resolution; paths a macro invocation generates (a blind spot) |
| Java | single-type imports (`import a.b.C;`, target the file declaring top-level type `C`; a nested `a.b.C.D` goes to `C`'s file); on-demand imports (`import a.b.*;`, target the files of package `a.b` whose type names the file uses); static imports; fully qualified names in code whose package is a repository package; same-package simple names (decision below) | the file that declares the type | `java.*`, `javax.*` and third-party types |

Three decisions, each a design choice [D]:

1. **Java same-package references are a rule, not a blind spot.** Java needs no import for
   a type in the same package, so a blind-spot treatment would make nearly every Java
   component `partial`. The rule: a capitalized identifier in file f that equals the simple
   name of a top-level type declared in f's package (the `.java` file names of that package
   directory under its source root, plus a scan for non-public secondary
   `class|interface|enum|record` declarations), and that no single-type import or type
   declared in f shadows, is an edge f -> the declaring file, kind `same_package`. When
   components are packages (the mechanical default, section 3) these edges stay inside one
   component and change no component edge; they matter when the cut falls at file grain
   or an owner map splits a package. The same identifier match resolves on-demand imports.
   The oracle can see it: jdeps keeps same-package dependences only with `-filter:none`
   ([TD]: "`-filter:package` Filter dependences within the same package. This is the
   default."). Measured [MH] on square/javapoet (one package, 17 files): 185 class-level
   same-package lines with `-filter:none`, 0 with the default. Assumption on every row: an
   identifier equal to a type name may name a variable or method instead; how often needs
   measuring (E13, section 4).
2. **A Rust `mod name;` is an edge, kind `mod`, in its own stratum.** The Python analogue
   is `from . import child` in a package `__init__`. A crate root names every top-level
   module, so the root's component gets an edge to every top-level module's component.
   These edges are real (the crate does not build without them) but carry no design choice
   beyond the file layout, so D1 items whose only sites are `mod` sites are a stratum of
   their own, as `type_only` is in [03 3.1].
3. **Re-exports: the target is the module the statement names, never the item's original
   definition.** Python X1 already does this (`from praxis import x`, re-exported by
   `__init__.py`, is an edge to `.`), tsc's resolver does it (it resolves the specifier,
   not the symbol), and Go and Java have no re-exports. Following re-exports to the
   definition would be a second relation, `through`, which is not built.

### 1.3 `module_level` and `type_only`, generalized

Today `module_level` on an edge means at least one site executes when the importing module
is imported [03 3.1]. The meaning per language:

| Language | `module_level` true | false |
|---|---|---|
| Python | unchanged (outside every `def`, not under `if TYPE_CHECKING:`) | unchanged |
| TypeScript, JavaScript | static `import`, `export ... from`, `import x = require()` (ES modules evaluate dependencies before the importer); `require()` outside every function body, including inside a top-level `if` or `try` block | `import()` always; `require()` inside a function or arrow body; `import defer` (TypeScript 5.9); every `type_only` site |
| Go | always: an imported package's `init` runs before the importer's | never |
| Java | always, read as "linked at compile time" (class initialization is lazy, so it does not mean "runs on load") [D] | never |
| Rust | the declaration is at module scope (Rust has no import-time execution, so this is scope, not execution) | a `use` inside a function or block body ("scoped") |

Measured [MH]: the TypeScript prototype's `deferred` flag (the complement of
`module_level`) agreed with the compiler-API oracle's function-ancestor test on 2,175 of
2,175 matched sites over ky, colinhacks/zod and expressjs/express (section 4.8).

Because the flag means different things, strata never pool across languages: a D1 report
stratifies by (language, `module_level`). An edge whose sites come from two languages
records `module_level` per language.

`type_only`: Python `if TYPE_CHECKING:`; TypeScript `import type`, `export type ... from`,
`import()` in a type position and `/// <reference path>`. Go, Rust and Java have no
type-only import form. An inline `import { type A, type B } from "s"` with no value
specifier is not `type_only` in v1, because under `verbatimModuleSyntax` the statement
survives as a side-effect import [D]; whether to change that is Q-L3.

### 1.4 Blind spots, per language

A blind spot is a site the extractor sees but cannot resolve statically, counted per
component as a lower bound and making items involving the component `partial`, as in
[03 3.1]. A specifier that resolves outside the repository is `external`, which is neither
an edge nor a blind spot.

| Language | Blind spots (reason code) |
|---|---|
| TypeScript, JavaScript | `import()` or `require()` with a non-literal argument (`dynamic`); `require.resolve`, `import.meta.resolve`, `new URL("./x", import.meta.url)`; aliases that only a bundler knows (Vite `resolve.alias`, webpack, babel module-resolver), seen as a bare specifier that matches no package and no `paths` entry but starts with `@/`, `~/` or a repository directory name (`alias_unconfigured`); `require.context`, `import.meta.glob`; JSDoc `import()` types inside comments; a relative specifier that resolves to no file, usually generated code not yet built (`relative_missing`) |
| Go | cgo; `//go:linkname`, which names another package's symbol with no import; `plugin.Open`; reflection; an in-module import path with no directory, usually a generated package not committed (`unresolved_local`). Build-constrained files are not blind: v1 takes the union over all constraints and the oracle does the same (section 4.2) |
| Rust | paths a macro invocation expands to (`macro_rules!` bodies are read as text, so `$crate::a::b` resolves; what an invocation produces is not seen); derive and other procedural macros; `mod` declarations inside a macro invocation's token tree (tokio's `cfg_rt! { mod runtime; }` pattern); `include!` of an `OUT_DIR` file (build-script output); `#[cfg(...)]` is not blind (union rule), and `#[cfg(test)]` items are excluded like tests (section 3.1) |
| Java | reflection (`Class.forName`, `loadClass`), counted even with a literal argument as Python's `importlib` is today; `ServiceLoader` and `META-INF/services`; dependency-injection classpath scanning (Spring `@ComponentScan`); annotation-processor and Lombok output: a reference to a repository package's type with no source file (`unresolved_local`) |
| All | generated code (committed: a file like any other unless marked, section 3.1; not committed: `unresolved_local` or `relative_missing`); calls across languages (an HTTP client in TypeScript calling a Python endpoint, FFI, a subprocess), which no extractor sees and every row's assumption names |

Measured [MH]: the TypeScript prototype counted 4 blind sites in zod's 523 files (two
non-literal `import()`, one computed `require()`, one `require.resolve`) and 3 in
express's 141.

## 2. The extractor interface

### 2.1 Layout: `src/praxis/lang/`

One module per language under `src/praxis/lang/` (`python.py`, `typescript.py`, `go.py`,
`rust.py`, `java.py`), with the registry and the protocol in `lang/__init__.py`. The flat
alternative (`x1_ts.py`, `x1_go.py`, ... beside `extract.py`) was considered and rejected
[D]:

- The hook imports only the language modules for the files a segment changed, chosen by
  suffix, so a Python-only repository never loads the TypeScript reader. Either layout
  allows that; the subpackage keeps the registry out of `extract.py`, which stays the
  shared lift.
- praxis's own mechanical map would see `lang` as one component, the concept the owner
  would name. Flat files would be four more depth-1 children, which only the flat-package
  grouping (used above 30 children, [03 2.2 step 5]) would join.
- The `judge-isolation` and `derived-writes-only-in-store` hooks match paths under
  `src/praxis/` and need no change either way.

Cost: praxis's own map gains a component, which opens a new `map_version` and a segment
break for praxis's own repository [03 2.4].

Python's site reader and module index (`_Sites`, `_Index`, `_module_names`,
`_relative_base`, `_candidates` in `extract.py`) move to `lang/python.py` behind the same
protocol, with identical behaviour, and the version stays `x1-v2`. The outside judge for
the move: `static_imports` returns a byte-identical `StaticGraph` on praxis, telltale and
systemap before and after, and the integration suite passes unchanged.

### 2.2 The protocol

```python
@dataclass(frozen=True)
class ImportSite:
    start: int             # byte offsets of the statement or expression
    end: int
    specifier: str | None  # None when the argument is not a literal (a blind site)
    kind: str              # closed vocabulary per language, Language.KINDS
    deferred: bool         # not run when the file is loaded (Rust: scoped to a body)
    type_only: bool        # erased before run time
    blind: str | None      # reason code when it cannot be resolved statically


@dataclass(frozen=True)
class Resolved:
    target: str | None     # repository-relative file (Go: a package directory)
    reason: str            # closed vocabulary: how it resolved, or why not


class Language(Protocol):
    NAME: str              # "typescript"
    VERSION: str           # "x1-ts-v1"
    SUFFIXES: tuple[str, ...]
    KINDS: frozenset[str]
    REASONS: frozenset[str]

    def files(self, repo_root: Path, listed: list[str]) -> list[str]: ...
    def roots(self, repo_root: Path, listed: list[str]) -> Roots: ...
    def imports(self, path: str, source: bytes) -> list[ImportSite]: ...
    def resolve(self, site: ImportSite, importer: str, roots: Roots) -> Resolved: ...
```

- `files` and `roots` take the one `git ls-files` listing that `extract.py` already makes
  (`_python_files`), so there is one subprocess per scan, not one per language.
- `roots` reads build files once per scan (section 2.4) and holds the listed file set, so
  `resolve` is set lookups with no I/O. That is what makes resolution fast and
  deterministic, and lets it run on a past tree.
- `imports` is pure: bytes in, sites out, no I/O. It can be timed and tested alone and run
  on a blob from `git show`.
- A Go target is a package directory. `extract.py` lifts it to the components of that
  directory's non-test `.go` files. The mechanical map never splits a directory (section
  3); when an owner map does, the edge goes to every component holding a file of the
  package, one site each, and the map carries the warning `split_package`.
- Privacy (invariant 5): the specifier text never leaves memory. Stored: the target path,
  component ids, counts and reason codes, as for Python today.

Reason vocabulary. Resolved: `relative`, `alias` (tsconfig `paths`), `base_url`,
`workspace`, `workspace_outdir`, `package_imports`, `module` (Go), `crate`, `crate_dep`,
`source_root` (Java), `same_package`, `reference`. Unresolved and not blind: `external`,
`asset`. Unresolved and blind: `dynamic`, `relative_missing`, `alias_unconfigured`,
`unresolved_local`, `ambiguous` (a name matching two files, as Python's two-file match is
counted today), `nul`.

### 2.3 What stays in `extract.py`

The listing, dispatch by suffix, the lift to components (`_Lift`, one site per statement),
`module_level` aggregation, `type_only` and blind-spot counting per component, parse
failures, `reach()`, X2 and X3. `StaticGraph` gains one field, `languages`: per language,
the files listed, parsed and failed and the extractor version, so coverage is visible
per language. `recorder.py`'s `_python()` filter becomes "files a registered language
claims", and `scan_files` and `_file_graph` are unchanged in shape.

### 2.4 Roots, from build files, standard library only

| Language | What `roots` reads | Parser |
|---|---|---|
| TypeScript, JavaScript | every `tsconfig.json` outside `node_modules`: `extends` (a string or a list; relative, or a package under `node_modules` when installed; a missing base is an issue on the roots, never a failure), `references`, `${configDir}` at the start of a value only, `compilerOptions` `paths`, `baseUrl`, `rootDir`, `rootDirs`, `outDir`, `customConditions`, `allowJs`, `moduleResolution`; the project for a file is the nearest `tsconfig.json` above it, with the references fallback [SM `typescript_projects.py`]; workspaces: every `package.json` outside `node_modules` with a `name` (a name two folders give is dropped as ambiguous [SM]) and its `exports`, `imports`, `types`, `main` | `json` after JSONC comment and trailing-comma stripping [SM `typescript_config.py`] |
| Go | `go.mod` `module` line per module directory (a nested `go.mod` makes its directory a separate module, excluded from the parent's); `go.work` `use` lines; `replace` lines with a local path | a line reader |
| Rust | `Cargo.toml`: `[workspace]` `members` (globs) and `exclude`; `[package]` `name` and `edition`; `[lib] path`, `[[bin]] path`, the auto targets (`src/lib.rs`, `src/main.rs`, `src/bin/*.rs`); `[dependencies]` entries with `path =`, and `package =` renames (the crate name in code has `-` as `_`) | `tomllib` |
| Java | source roots inferred from package declarations: a file at `X/a/b/C.java` declaring `package a.b;` gives the root `X`; two inferences for one directory are an issue. The Maven and Gradle convention (`src/main/java`) is a cross-check only; E13 uses the convention alone [E13 `_java_roots`]. Inference covers Gradle `sourceSets` overrides without reading Groovy or Kotlin build scripts | a line reader over each file's first declaration |

Calling `cargo metadata`, `go list` or node at Stop was rejected: the hook would depend on
a toolchain being installed, and the process alone costs more than the budget. Measured
[MH], five or three runs each: `node -e 0` 31 to 80 ms; node loading the TypeScript
package 299 to 492 ms; warm `go list -e -json ./...` on fzf 190 to 739 ms; `cargo metadata
--no-deps --offline` on fd 61 to 110 ms.

### 2.5 The TypeScript reader without a parser

systemap parses TypeScript with tree-sitter, an optional dependency in its
`pyproject.toml` [SM `typescript_syntax.py`], which praxis cannot use (invariant 6). The
reader is a hand lexer in two passes:

1. Tokens. Comments, strings, template literals with nested `${...}`, regular-expression
   literals told from division by the previous significant token, and JSX told from
   comparison by the previous token in `.tsx`, `.jsx` and `.js` files (never in `.ts`,
   where `<T>x` is a type assertion). Runs of code holding no token of interest become one
   bulk token, which keeps the per-token Python cost low.
2. Statements. An `import` not preceded by `.` and not followed by `(` or `.` starts a
   static import; `export` followed by `{`, `*` or `type {` scans to `from`; `require` and
   `import` followed by `(` take one string literal or count a blind site. A brace stack
   marks function bodies (a `{` after `=>`, or after a `)` whose `(` did not follow `if`,
   `for`, `while`, `switch`, `catch` or `with`), which gives `deferred`.

Copied from systemap and kept: tsconfig reading (JSONC, `extends` chains including arrays
and `node_modules`, `${configDir}`, `paths` priority and `baseUrl`); tsc's extension
substitution table (`.js` to `.ts` then `.tsx`, `.mjs` to `.mts`, `.cjs` to `.cts`, `.jsx`
to `.tsx` then `.ts`) and index files [SM `typescript_projects.py`]; the `package.json`
`exports` and `imports` algorithm (exact keys first, then the pattern with the longest
prefix, `null` blocking, conditions in key order) [SM `typescript_package_imports.py`]; the
workspace name map [SM `typescript_resolve.py`]; the refusal of paths that leave the
repository, links out and FIFOs [SM `repo_files.py`]. Changed: systemap tries only
TypeScript sources (`PLAIN_SOURCES = (".ts", ".tsx")`), and praxis adds `.js`, `.jsx`,
`.mjs` and `.cjs` files and their resolution; systemap maps imports to dotted module
names, praxis to files; praxis adds `require()`, `import()` and `deferred`.

## 3. Product roots and components, per language

The rules of [03 2.2] stay: fixed classes, a product root, the smallest depth d giving at
least 5 product components with a cap of 30, `<dir>/.` for a file beside subdirectories,
and flat-package grouping. What changes:

### 3.1 Fixed classes gain per-language members

| Class | Added members |
|---|---|
| `tests` | `*.test.*`, `*.spec.*`, `__tests__/`, `test-d/`, `*.test-d.ts` [SM `typescript_roots.py`]; `*_test.go`, `testdata/`; Rust `tests/`, `benches/`; Java `src/test/`. Rust `#[cfg(test)]` items cannot be split by path, so their sites are excluded from edges and counted per component as `test_only` |
| `config` | `package.json`, `tsconfig*.json`, `*.config.{js,ts,mjs,cjs}`, `go.mod`, `go.sum`, `go.work`, `Cargo.toml`, `Cargo.lock`, `build.rs`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle*`, `gradlew` |
| `examples` (new) | `examples/`, `example/`: recorded, never forecast, never in items |
| `vendored` (new) | `vendor/`, `third_party/`, a tracked `node_modules/` |
| `generated` (new) | a file whose first lines hold Go's marker (`// Code generated ... DO NOT EDIT.`), `@generated`, or that `.gitattributes` marks `linguist-generated`; read at derive time, never at Stop |

### 3.2 Product roots

A list, one per package: Python, today's rule; TypeScript, each workspace package's source
folder by systemap's root rule (the one folder `include` names, else `src`, else the
package directory) [SM `typescript_roots.py`]; Go, each module directory; Rust, each
workspace member's `src/` (or its crate directory when targets live elsewhere); Java, each
inferred source root, descended through single-child directories (the reverse-domain chain
`com/example/app`) to the first directory with two children or a file.

### 3.3 The depth rule over several roots

With one product root, unchanged. With several, the roots become the depth-1 children of
a virtual root, each named by the shortest suffix of its path that is unique, and the same
smallest-d rule runs [D]. A monorepo with 8 packages gets 8 components at d = 1; one with
3 packages descends to package/child at d = 2. The 5 and 30 bounds and freezing are
unchanged.

### 3.4 How the rule fits each language

- Go: a directory is a package, and the cut keeps directories whole (a path deeper than d
  belongs to its directory at depth d). A file directly in a directory that has
  subpackages goes to `<dir>/.`, which is Go's own meaning: that directory's package. So
  the mechanical map never splits a Go package.
- Rust: modules are files or directories, so the same cut applies; a crate root
  `lib.rs` or `main.rs` is `.`.
- Java: packages are directories. At directory grain same-package edges stay inside a
  component; in a single-package repository the cut falls at file grain (javapoet: 17
  files, so 17 file components) and the `same_package` rule supplies the edges.
- TypeScript: directories under the source folder; a barrel `index.ts` is `<dir>/.`.
- Flat packages: grouping by the token before the first underscore suits Python's
  snake_case. TypeScript files are often kebab-case or camelCase and Java is CamelCase, so
  v1 splits at the first `_`, `-`, `.` or lowercase-to-uppercase boundary [D]. Its fit needs
  measuring: `experiments/E03/flat_groups.py` on one flat TypeScript directory before the
  TypeScript extractor lands (Q-L6).

### 3.5 Polyglot repositories

The map is paths only and stays one map. Section 6 says how several languages give one
edge set.

## 4. The oracle plan

### 4.1 Principle

Each extractor must agree with its language's own toolchain, run in a scratch clone and
never in the owner's tree, on repositories it was not tuned on. Oracles are dev-only: they
live in `experiments/E13/`, never in `src/praxis/`, never run in a hook, and may use node,
go, cargo, rustc, rust-analyzer, javac and jdeps. Extraction (which sites) and resolution
(which file) are compared separately, so a disagreement names its layer.

### 4.2 Oracles, per language

**TypeScript and JavaScript.**

- Primary, the compiler-API route: a node script loading the TypeScript package walks each
  file's `ts.createSourceFile` tree for `ImportDeclaration` (`importClause.isTypeOnly`),
  `ExportDeclaration` with a module specifier (`isTypeOnly`), `ImportEqualsDeclaration`
  with an `ExternalModuleReference`, a `CallExpression` on `ImportKeyword` or on the
  identifier `require` with one string-literal-like argument, `ImportTypeNode` with a
  string literal, and `sourceFile.referencedFiles`. `deferred` is a dynamic import or a
  function-like ancestor. Resolution is `ts.resolveModuleName(specifier, file, options,
  ts.sys, undefined, undefined, mode)`, with `options` from
  `ts.getParsedCommandLineOfConfigFile` on the nearest `tsconfig.json` and `mode` from
  `ts.getModeForUsageLocation`; targets pass through `fs.realpathSync`, so workspace
  links land on repository files. A repository with no `tsconfig.json` gets a generated
  one (`allowJs`, `checkJs` false, `nodenext`), as E13 does [E13 `_ts_config`]. This is
  the route the prototype was measured against.
- Cross-check of the oracle itself, the trace route: `tsc -p <tsconfig> --noEmit
  --traceResolution`, pairing each `======== Resolving module 'S' from 'F'. ========` with
  the next `======== Module name 'S' was successfully resolved to 'T'` or `was not
  resolved` line, and dropping resolutions with no site. Measured [MH] on ky (98 files,
  2.25 s): 125 trace blocks; 94 (importer, specifier) keys shared with the API route, 94
  agree; the other 31 are all `react/jsx-runtime`. E13's rule for choosing between the
  routes is written in its own docstring [E13].
- Environment: dependencies installed with the lockfile's own tool and scripts off
  (`npm ci --ignore-scripts`, `pnpm install --frozen-lockfile --ignore-scripts`, `yarn
  install --immutable`), so that workspace links and shared tsconfig bases exist. Measured
  why [MH]: on zod with nothing installed the oracle resolved 603 of the 672 product-file
  edges the prototype found; the other 69 were imports of another workspace package,
  which tsc resolves only through a `node_modules` link. With the 8 workspace links made by
  hand: 672 of 672.

**Go.** In each module directory, `go list -e -json ./...` (E13 adds `-deps` [E13]); per
package `ImportPath`, `Dir`, `GoFiles`, `CgoFiles`, `IgnoredGoFiles`, `Imports`, filtered
to in-repository import paths. It runs under three `GOOS/GOARCH` pairs (linux/amd64,
darwin/arm64, windows/amd64) and the union is the oracle, matching the extractor's union
rule; files still in `IgnoredGoFiles` after the union are custom-tag files, reported as
`tag_only`. File grain comes from E13's `go/parser` helper (`go run imports.go`) [E13]. The
gate is at package grain (directory to directory). Measured [MH] on junegunn/fzf (55c122f,
91 tracked `.go` files): 6 packages, 7 in-module package edges, the prototype reader 7 of
7, 16 `IgnoredGoFiles` adding no in-module edge, `go list` 0.84 s on its first run, including module
downloads, and 190 to 739 ms warm.

**Rust.** Three tools, no nightly:

- Crate graph: `cargo metadata --format-version 1 --no-deps` (members, targets with
  `src_path`, editions, path dependencies), as E13 does [E13]. 85 ms on fd [MH].
- Module membership: dep-info. `cargo check --message-format=json` writes
  `target/debug/deps/<crate>-<hash>.d`, listing every source file the crate compiled under
  the active `cfg`; on fd, 22 files for the binary target (97 s cold, including dependency
  downloads and builds) [MH]. E13 runs `rustc --emit=dep-info` per target instead [E13];
  either works. Dep-info checks the `mod` tree, `#[path]` and `cfg`'d modules, and holds
  no edges.
- Module-level edges: rust-analyzer's `lsif` subcommand on the stable toolchain, JSON lines
  of every reference range with its definition range and document. The oracle script
  drops references preceded by `.` (method or field, outside the construct), maps
  `crate`, `self` and `super` keywords to the module they name, maps module references to
  the module's file through dep-info, and keeps item references. Measured [MH] on fd: 16.0
  s and 53,776 lines; 85 raw file pairs, 80 without method and field references; all 39
  `crate` keywords resolve to `src/main.rs` and would add 14 spurious pairs if not mapped.
- Rejected: `-Zunpretty=expanded` and `hir` (nightly only; `RUSTC_BOOTSTRAP=1` makes them
  run on stable but is an unsupported switch, and the output is expanded source, not
  resolved references, so praxis's own reader would be grading itself); rustdoc JSON
  (nightly, items only, no function bodies); save-analysis (gone: `RUSTC_BOOTSTRAP=1 rustc
  -Z help` on 1.97 lists no such option [TD]); dep-info alone (membership, no edges).
- Audit of the oracle, because its filters are assumptions: every crate pair LSIF connects
  must be a declared dependency in `cargo metadata`, and every LSIF document of a crate
  must be in that crate's dep-info. E13 must pin the rust-analyzer version.

**Java.** Compile with the repository's build (`mvn -q -DskipTests compile`, `gradle
classes`), or for sources with no third-party dependency `javac -d <out> -proc:none
-nowarn -encoding UTF-8` over the source roots, as E13 does [E13 `_javac`]. Edges:
`jdeps -verbose:class -filter:none <classes>`; a class maps to its source file by its
package and the class file's `SourceFile` attribute (E13 reads the constant pool [E13]),
and a nested `A$B` maps to `A`'s file. Measured [MH] on javapoet (b9017a9): `javac` 3.06 s,
`jdeps` 0.87 s, 828 dependency lines with `-filter:none` against 643 by default. Bytecode
cannot hold two kinds of reference, so these classes are fixed now, before data, and
removed from both sides before scoring, with their counts reported:

- J1: a single-type import whose type the file uses only inside comments (Javadoc
  `{@link}`).
- J2: a reference to a compile-time constant, which javac inlines (Java Language
  Specification 13.1).

No other exclusion is permitted after data.

### 4.3 Comparison rules, written before data

- Product files only: the repository's mechanical map's fixed classes (section 3.1) are
  dropped on both the source and the target side.
- Grain: file to file for TypeScript, JavaScript, Rust and Java; package directory to
  package directory for Go (the extractor's file-to-package edges unioned per package).
- Precision and recall over distinct edges; site counts compared separately.
- Sites both sides leave unresolved, when the specifier looks local (relative, an alias
  pattern, the module path, `crate::`), are counted per reason. A shared miss is
  invisible to precision and recall, so it is reported beside them.
- Rust `#[cfg(...)]` sites other than `test` are tagged `cfg_gated` by the extractor. E13
  runs the oracle with `--all-features` where the build allows. A `cfg_gated` edge only
  the extractor has is reported and removed from the precision count only, never from
  recall, because the oracle sees one configuration and the extractor the union.
- Component grain: both edge sets lifted through the repository's mechanical map.
- TypeScript sites matched by (file, line, specifier, kind) are compared on `deferred`
  and `type_only`.

### 4.4 What E13 must output so the two agree

E13's `Oracle` already holds file edges, directory edges, an unresolved count and seconds
[E13]. For language acceptance it must emit, per (language, repository, commit), one JSON
object:

```
language, repo, url, commit, map_version
oracle: route, tool_versions, install_command, seconds, diagnostics
extractor: version, seconds, per_file: [path, bytes, ms]
grain: "file" | "package"
edges:           oracle, extractor, both, precision, recall, precision_ci95, recall_ci95
component_edges: oracle, extractor, both, precision, recall
sites: oracle, extractor, matched, deferred_agree, type_only_agree
disagreements: [src, dst, side, class]
both_unresolved: {reason: count}
excluded: {J1: n, J2: n, cfg_gated: n}
verdict: "pass" | "fail", rule: "04 section 4.5"
```

Disagreement classes, a closed list: `extract_miss` (an oracle site with no extractor
site), `extract_extra`, `resolve_miss` (same site, extractor unresolved), `resolve_wrong`
(different target), `resolve_extra`, `oracle_env` (the oracle could not resolve for a
missing install or build output), `out_of_construct`. Intervals are Wilson 95 percent.
Paths of public repositories in a scratch clone are not the owner's data, so the
disagreement list may name them. What E13 still has to add, as of [E13]: site records,
the product-file filter, `both_unresolved` by reason, the `GOOS` union, the LSIF route for
Rust module edges (its Rust oracle is crate to crate today), and per-file extractor
timing.

### 4.5 Acceptance rule, per language, written before any extractor data

`x1-<lang>-v1` lands only when, on each of its five held-out repositories separately and
never pooled:

- A1. Edge precision at least 0.98 and recall at least 0.98 at the grain of 4.3. Each
  repository must hold at least 200 oracle edges (Go: 50 package edges), so 0.98 allows at
  most 4 misses on either side (Go: 1); a repository below the minimum is replaced before
  the extractor runs on it.
- A2. TypeScript and JavaScript: `deferred` and `type_only` agree on at least 0.98 of
  matched sites.
- A3. Component-grain precision and recall at least 0.98 under the repository's mechanical
  map, gated because P1's truth is at component grain.
- A4. Budget. Over the repository's product files, warm process, roots built once: per-file
  read plus resolve p95 at most 10 ms; median ms per KB at most X1-Python's on praxis's
  `src/praxis` in the same E13 run; and E04 rerun with one file of the language touched
  adds at most 100 ms to the Stop's wall-clock p95 over the same Stop without a frozen map
  (E04's own rule [E04]).
- A5. Zero extractor exceptions, and zero parse failures on files the oracle parsed.

A failure names the repository, the class and the count. The fix is a new extractor
version, and the five repositories are replaced by five new ones, because a repository an
extractor was fixed on is no longer held out [D]. E13's Python rule (recall at least 0.95
against a runtime trace) is a different gate on a different oracle and is unchanged.

Why 0.98 [D]: X1 is the truth of the primary outcome. At the measured D1 densities (17
edges among 90 pairs on the 10-component tree, 14 among 182 at e9b5e3a [03 3.1]) a
repository has tens of component edges, and 0.98 at file grain keeps the expected number
of wrong component edges per repository below one. Whether it does is what A3 checks.

### 4.6 Repositories

Any repository used here or in E13's route choice is excluded, because it was seen: ky,
zod, express, fzf, fd, javapoet and pmndrs/zustand. Candidates, each public on github.com
and clonable over HTTPS (every clone tried here succeeded [MH]); the commit is pinned in
the write-up, sizes and edge counts need measuring against A1's minimum, and a candidate
whose oracle cannot run is replaced by the next one named in the write-up before the
extractor runs on it, never after:

| Language | Repository | Why |
|---|---|---|
| TS/JS | webpack/webpack | CommonJS JavaScript, `require` throughout, JSDoc types; large |
| TS/JS | vitejs/vite | pnpm monorepo, ES-module TypeScript, package `exports` |
| TS/JS | excalidraw/excalidraw | TSX and JSX (the lexer's hardest input), yarn workspaces, aliases |
| TS/JS | trpc/trpc | monorepo with tsconfig `paths` and `exports` conditions |
| TS/JS | date-fns/date-fns | very many small files and barrels |
| Go | cli/cli | `cmd/`, `internal/`, `pkg/` layout |
| Go | gohugoio/hugo | large, many packages, build tags |
| Go | prometheus/prometheus | nested modules |
| Go | hashicorp/consul | several modules joined by local `replace` |
| Go | kubernetes/kubernetes | `vendor/`, staging modules by `replace`, generated code, build tags: the stress case |
| Rust | BurntSushi/ripgrep | workspace crates and path dependencies |
| Rust | sharkdp/bat | one crate, `build.rs`, features |
| Rust | tokio-rs/tokio | `cfg` macros wrapping `mod` declarations |
| Rust | serde-rs/serde | procedural-macro crate, `$crate` paths |
| Rust | helix-editor/helix | many crates, medium to large |
| Java | google/gson | Maven multi-module, no dependency in the core |
| Java | apache/commons-lang | Maven, one module |
| Java | google/guava | large packages: `same_package` at scale |
| Java | jhy/jsoup | Maven, no dependencies |
| Java | junit-team/junit5 | Gradle Kotlin DSL, Java mixed with Kotlin: the unmapped-language share |

### 4.7 The parse budget, measured

The baseline is X1 on Python, `ast.parse` plus the `_Sites` walk, timed in-process [MH]:

| File | Bytes | Runs | Parse p50 | Parse+walk p50 | Parse+walk p95 |
|---|---|---|---|---|---|
| `src/praxis/store.py` | 34,165 | 200, 300, 300 | 5.06 to 5.22 ms | 8.84 to 9.00 ms | 10.57 to 12.22 ms |
| `src/praxis/report.py` | 31,444 | 200, 300, 300 | 7.07 to 7.33 ms | 12.71 to 13.45 ms | 17.91 to 19.79 ms |

Over all 21 files of `src/praxis` (50 runs each, two runs of the whole set): median of
per-file medians 5.22 and 5.36 ms, maximum 21.18 and 16.53 ms, 4 and 6 files over 10 ms
(probe.py, recorder.py, report.py and store.py in the first run), and 0.434 and 0.432 ms
per KB at the median for files over 4 KB. So the brief's "under 10 ms per changed file" is
not met today by the Python extractor on praxis's own largest files. That is why A4 states
the budget as a p95 over a repository's product files, a per-KB ratio to X1-Python and
E04's whole-Stop rule, rather than a per-file maximum. A file over 512 KB (a committed
bundle) is skipped as `too_large` and counted as a parse failure [D]; the right cut needs
measuring as the largest product file across the twenty repositories of 4.6.

### 4.8 The TypeScript prototype, measured against the oracle [MH]

Repositories: sindresorhus/ky (3541888; 98 `.ts` files), colinhacks/zod (a66e698; 477
`.ts`, 34 `.tsx`, 5 `.mjs`, 3 `.cts`, 2 `.js`, 1 `.mts`, 1 `.cjs`), expressjs/express
(0dee2c5; 141 `.js`). Oracle: the compiler-API route of 4.2. Sites keyed by (file, line,
specifier, kind), every file including tests:

| Repository | Oracle sites | Prototype sites | Agree | `deferred` agree | `type_only` agree | Blind (prototype) |
|---|---|---|---|---|---|---|
| ky | 311 | 311 | 311 | 311 | 311 | 0 |
| zod | 1,464 | 1,464 | 1,464 | 1,464 | 1,464 | 4 |
| express | 400 | 400 | 400 | 400 | 400 | 3 |

Import forms the oracle found: ky 236 `import`, 50 `import type`, 13 `export from`, 8
`export type from`, 4 `import()`; zod 1,103 `import`, 208 `import type`, 131 `export
from`, 9 `import()`, 7 `export type from`, 3 side-effect `import`, 2 `require`, 1 type
`import()`; express 400 `require`, 1 of them inside a function. No `import x =
require()` and no `/// <reference path>` appeared in the three.

Resolution, every file including tests:

| Repository | Site targets agree | Distinct file edges, prototype | Oracle | Both |
|---|---|---|---|---|
| ky | 311 of 311 | 182 | 182 | 182 |
| zod (workspace links present) | 1,464 of 1,464 | 934 | 934 | 934 |
| express, first run | 304 of 400 | | | |
| express, after the fix | 400 of 400 | 153 | 153 | 153 |

The express first run is the oracle doing its job: the prototype could not resolve
`require("../..")`, a require of the repository's root directory, on 96 sites; a two-line
fix in the root-directory check gave 400 of 400, and ky and zod were rerun after it with
unchanged results. Two shared misses are not visible in these tables: ky's `test-d` files
import `ky` by its own name, which `exports` points at an unbuilt `distribution/` folder;
both sides leave it unresolved (red-team row R5).

Cost, in-process. Lexer versions on ky's largest file (`test/hooks.ts`, 195,390 bytes),
system Python 3.13.16: character loop 196.58 ms, master regular expression 100.47 ms, bulk
tokens 65.04 ms. Read plus resolve per product file (tests excluded), bulk-token version,
roots built once, median of 5 per file, praxis's Python 3.12.13, two runs:

| Repository | Product files | Roots build | p50 | p95 | Max | ms per KB (median, files over 4 KB) | Files over 10 ms |
|---|---|---|---|---|---|---|---|
| ky | 31 | 3.4 to 4.6 ms | 0.28 to 0.34 ms | 1.79 to 2.44 ms | 11.24 to 11.85 ms | 0.158 to 0.170 | 1 (53,421 bytes) |
| zod | 319 | 7.7 to 8.0 ms | 0.90 to 0.94 ms | 5.82 to 7.44 ms | 46.52 to 49.88 ms | 0.267 to 0.289 | 9 to 12 |

So the prototype costs about 0.16 to 0.29 ms per KB against X1-Python's 0.43, and meets
A4's per-file p95 on both repositories. The oracle took 1,461 ms on ky's 98 files and
2,010 ms on zod's 523.

## 5. X2 beyond Python

X2 records which components a named scenario enters and which calls into which [03 3.2].

- **Node: feasible with no dependency.** `NODE_OPTIONS="--require <tracer.cjs>"` reaches
  every node process a scenario starts, the analogue of X2's `sitecustomize` trick. The
  tracer calls `module.registerHooks` (present in node 22.22 here), whose resolve hook sees
  every resolution with its parent URL. Measured [MH] on a four-file fixture, it recorded
  the static ES import, the dynamic `import()` and the CommonJS `require`. Wrapping
  `Module._load`, as first proposed, sees CommonJS only. `NODE_V8_COVERAGE=<dir>` makes
  V8 write per-script, per-function execution counts at exit for every process, with no
  instrumentation; measured on the same fixture, every function with its count. So Node
  gives runtime import edges (the V2 check of [03 8] for TypeScript) and components
  entered (`RT` entered). It does not give call edges: `--cpu-prof` samples stacks, so an
  edge it misses is not absent. RT call items stay `unknown` for Node until sampled
  profiles are shown stable across repeat runs (Q-L10).
- **Go, Rust, Java: no generic tracer without changing the owner's build.** `go build
  -cover` with `GOCOVERDIR` gives block coverage of a binary the scenario builds; Rust's
  `-C instrument-coverage` needs the llvm-tools component (not installed here); Java's
  built-in `-Xlog:class+load` gives classes loaded, which is not entered, and JFR samples.
  These can serve as oracles of "entered" at most.

X2 stays Python and Node for now, because those two runtimes offer standard-library
praxis code a hook into an unmodified program (`sitecustomize`; `NODE_OPTIONS` with
`registerHooks` and `NODE_V8_COVERAGE`). The others need the owner's program rebuilt with
instrumentation, which is a different design with its own scratch build caches.

## 6. Migration

- **Versions.** Python keeps `x1-v2`: renaming it would orphan the `file_edges` rows
  already keyed by it. New: `x1-ts-v1`, `x1-go-v1`, `x1-rs-v1`, `x1-java-v1`. A
  `file_edges` row's `extractor_version` is its source file's language version; the
  column exists, one row per source file. A component-grain `StaticGraph` carries the
  sorted join of the versions that contributed (`x1-ts-v1+x1-v2`) [D]. P1 truth stores it,
  and truth under two different joins is never pooled, as `map_version` is not.
- **One map, one edge set.** Each language scans its own files from the one listing; all
  sites lift through the one map and sum per component pair; an edge keeps per-language
  site counts and per-language `module_level`. An edge between two languages is not seen,
  and every row's assumptions say so.
- **`praxis set components` reports the languages.** Every product file is classified by
  suffix into a covered language (with its extractor version), an uncovered programming
  language (a closed table: `.kt`, `.kts`, `.swift`, `.rb`, `.php`, `.cs`, `.scala`, `.c`,
  `.h`, `.cc`, `.cpp`, `.hpp`, `.m`, `.mm`, `.ex`, `.exs`, `.clj`, `.dart`, `.lua`, `.jl`,
  `.hs`, `.ml`, `.vue`, `.svelte`, and the rest), non-code (`.json`, `.yaml`, `.toml`,
  `.css`, `.html`, `.svg`, `.sql`, images), or an unknown suffix, counted on its own line
  and never folded into another. It prints files and share per class. A file in an
  uncovered language is `unmapped language`, never silently absent. A component holding
  any such file has coverage `partial`; one holding only such files is `unknown`, and no
  item involving it is generated.
- **The recorder.** Edges at Stop run for every file a registered language claims; over
  budget the boundary's edges are `unobservable` [03 3.4]; nothing else in the recorder
  changes.
- **Until a gate passes, nothing else changes.** No store table, probe, report text or
  measure mentions a language before its extractor lands. [03 2.3] stays true for every
  language that has not landed.

## 7. Red team

Each row carries its assumption text on every Evidence row it affects.

| # | Language | How the extractor can be wrong | What the oracle catches | Assumption text |
|---|---|---|---|---|
| R1 | TS/JS | Barrel `index.ts` re-exports: the edge goes to the barrel's component, not the definition's | Nothing to catch: tsc resolves the specifier too, so both agree by design (decision 3) | "An edge names the module a statement names; a re-exported definition's own component is not reached." |
| R2 | TS/JS | tsconfig `paths`, `baseUrl`, `extends` misread | All: tsc reads the same configuration | none beyond the shared one |
| R3 | TS/JS | Aliases only a bundler knows | Nothing: tsc fails too, a shared miss, counted in `both_unresolved` | "Imports through bundler-only aliases are blind spots." |
| R4 | TS/JS | Workspace package not installed or not built | `oracle_env` in E13; the extractor resolves workspace names itself, so it is right where the oracle is not | "A workspace import resolves to the package's source, not its build output." |
| R5 | TS/JS | A self-import through `exports` into an unbuilt output folder, whose source mapping depends on a build tsconfig that is not `tsconfig.json` (ky: `tsconfig.dist.json`) | Nothing: both unresolved, observed on ky's `test-d` files [MH] | "A self-import into unbuilt package output is unresolved." |
| R6 | TS/JS | Lexing: regular expression against division, quotes in JSX text, nested templates | All, through site comparison: 0 disagreements in 2,175 sites over 762 files [MH] | none |
| R7 | TS/JS | A type-position `import()` read as a dynamic import (v1 treats only `typeof import(` as a type) | All: the oracle sees `ImportTypeNode` (zod's one matched [MH]) | none |
| R8 | TS/JS | JSDoc `import()` types in JavaScript comments | Nothing: the API route's walk does not visit JSDoc either | "Type references in JSDoc comments are not edges." |
| R9 | Go | `internal/` visibility; vendored copies of in-repository modules; staging modules reached by `replace` (kubernetes) | `replace` and `go.work` errors: `go list` reports each package's `Dir`; `internal/` violations do not compile, so they do not occur in a building repository | "Third-party code under vendor/ is external." |
| R10 | Go | Build constraints | GOOS and GOARCH: the oracle's union; custom tags: reported `tag_only` | "Edges are the union over build constraints." |
| R11 | Go | Generated packages not committed (`go:generate`, protobuf) | Nothing: both fail, shared | "An import of an absent in-module package is a blind spot." |
| R12 | Rust | `#[cfg]` items: the extractor takes the union, the oracle sees one configuration | Recall: yes; precision: `cfg_gated` extras are reported apart (4.3) | "Edges are the union over cfg; items under cfg(test) are excluded." |
| R13 | Rust | Macro-generated paths; `mod` inside a macro invocation (tokio) | Yes: rust-analyzer expands macros, so these show as recall misses; tokio is in the set to measure them | "Paths a macro invocation generates are not seen." |
| R14 | Rust | Method and field references | Filtered out of the oracle: outside the construct (5 of 85 pairs on fd [MH]) | "Method calls are not edges." |
| R15 | Rust | 2015-edition paths (crate-relative `use`) | Yes: a wrong edition gives `resolve_wrong` | none |
| R16 | Rust | Glob imports `use super::*` | Partly: LSIF names items actually used, possibly in other files through re-exports | "A glob import is an edge to the module it names." |
| R17 | Java | The `same_package` rule matching a variable or method named like a type | Yes: `jdeps -filter:none` | "A same-package simple name is assumed to name the type." |
| R18 | Java | Reflection, `ServiceLoader`, dependency-injection scanning | Nothing: bytecode does not hold it either; shared | "Reflective and container wiring is not seen." |
| R19 | Java | Imports used only in Javadoc; inlined constants | The oracle misses them; classes J1 and J2 fixed before data (4.2) | none |
| R20 | Java | Non-public top-level types in another file; nested types | Yes: the `SourceFile` attribute maps each class to its file | none |
| R21 | All | Committed generated code (`.pb.go`, OpenAPI clients) that the marker test misses | Nothing: the edges are real; the class is structural, not a correctness issue | "Generated files without a marker are treated as written code." |
| R22 | All | Case: on a case-insensitive file system (macOS) an import whose case differs from the file resolves; the extractor matches the case-sensitive git listing | Nothing on this Linux container | "An import resolves only when its case matches the listed path." The owner's file system needs checking |
| R23 | All | Symlinked directories committed inside the repository | Partly: the oracle follows `realpath`, the extractor the listing | "A path is the listed path; links are not followed." |
| R24 | All | One statement naming two files of one component counted twice | Not an oracle question: the shared `_Lift` counts one site per statement, as the recorder does today [E04] | none |

## 8. Build order, with gates

The order below is a guess. It holds until E11's language inventory replaces it: E11
(`docs/preregistration.md` 1.3.1) reads the owner's transcripts, and gains one count, per
language by suffix, of the paths named by mutating tool calls, plus the share of tracked
product files per language in each active repository, counts only (invariant 5). The
language with the most agent-written paths goes first among those whose oracle runs on
this container.

0. **Refactor, no new language.** Python's reader into `lang/python.py` behind the
   protocol. Gate: byte-identical `StaticGraph` on praxis, telltale and systemap, and the
   integration suite. Then E13 grows the output of 4.4.
1. **TypeScript and JavaScript.** The oracle exists here: tsc 6.0.2 and node v22.22.0.
   Build `x1-ts-v1` from the prototype; run E13 on the five repositories of 4.6. Lands
   only when E13 reports A1 to A5 met on all five. Then Node X2 (section 5).
2. **Go** (go1.24.7 here). Same gate.
3. **Rust** (cargo and rustc 1.97.0 here; E13 fetches and pins rust-analyzer, since the
   component is not installed). Same gate.
4. **Java** (javac and jdeps 21.0.12.1 here; `mvn` and `gradle` under `/opt`). Same gate.

Nothing else in praxis changes until a language lands.

## 9. Open questions

| # | Question | Measurement that settles it |
|---|---|---|
| Q-L1 | Which languages the owner's agents actually write, so the build order | E11's language inventory on the owner's machine (section 8) |
| Q-L2 | Whether a stdlib lexer holds at 0.98 on TSX-heavy and very large repositories | E13 on excalidraw and webpack (A1, A2); the prototype has seen only 34 `.tsx` files |
| Q-L3 | Whether an inline `import { type A }` with no value specifier should be `type_only` | E13: count such statements, and compare with whether tsc's emitted JavaScript keeps the import under each repository's own settings |
| Q-L4 | How often a Java simple name equal to a same-package type names something else | E13 `disagreements` of class `extract_extra` with kind `same_package`, on guava |
| Q-L5 | How often an owner map splits a Go package (`split_package`) | `praxis set components` warnings on the owner's Go repositories once an owner map exists |
| Q-L6 | Whether the widened name-token split groups flat TypeScript and Java directories as well as it groups Python | `experiments/E03/flat_groups.py` on one flat directory per language, against the owner's grouping where one exists |
| Q-L7 | Share of TypeScript imports through bundler-only aliases on the owner's repositories | Count of `alias_unconfigured` per repository in `praxis set components` output |
| Q-L8 | Stop cost when one segment changes many files in several languages | E04 extended: segments touching 1, 10 and 50 files across two languages |
| Q-L9 | Share of committed files that are generated, and how many the marker test finds | Count of `generated` per repository against a hand list of known generated paths in the twenty repositories |
| Q-L10 | Whether Node `--cpu-prof` call edges are stable enough to give RT call items | Repeat each Node scenario 10 times and measure the share of call edges seen in every run |
| Q-L11 | Whether the TypeScript rule that a relative import with no file is a blind spot (`relative_missing`) should reach Python X1 (an `x1-v3`) | Count of unresolved local-looking Python imports on praxis, telltale, systemap and the owner's repositories |
| Q-L12 | Share of Rust crates still on the 2015 edition | `edition` fields across the five Rust repositories and the owner's crates |
| Q-L13 | Whether the owner's machine is case-insensitive (R22) | `praxis show status` checks the file system of each active repository |
| Q-L14 | Whether LSIF's method and field share on fd (5 of 85 pairs) is typical | E13's `out_of_construct` counts on the five Rust repositories |

## 10. Design record

- Rejected: tree-sitter or any parser package (invariant 6, and systemap already shows the
  cost: an optional extra); a language server or the toolchain at Stop (process start
  alone is 31 to 492 ms, section 2.4, and the hook would depend on what is installed);
  line regular expressions without a lexer (strings, comments, templates and JSX produce
  false sites).
- Kept from systemap, re-implemented, not imported: the tsconfig reader, the extension
  table, the `exports` and `imports` algorithm, workspace names, path refusal.
- Added beyond the brief: one oracle cross-check per language (the trace route for
  TypeScript, metadata and dep-info for Rust), J1 and J2 fixed before data, `cfg_gated`
  scored for recall only, the `examples`, `vendored` and `generated` classes, and the
  exclusion of every repository seen here from the acceptance set.
