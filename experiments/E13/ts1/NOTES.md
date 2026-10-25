# ts1: step 1 of design 04 section 8, x1-ts-v1
Worktree: /home/user/praxis/.claude/worktrees/ts (branch lang/typescript, base 6da4358). Never commit.
## Log
- 1. Started fresh. Read design 04.
- 2. Read everything. Decisions (before data):
  - components.py FIXED NOT changed (map_version hashes FIXED: changing it changes every repo's map_version, a Python result). Design 3.1 TS fixed classes live in E13 (product filter + A3 map built by components.derive over tracked files minus 3.1 TS classes). 3.2 multi-root not built.
  - systemap clone in step0/repos has 15 TS/JS files: judge must compare Python-only graph (files=python list) and a deregistered run.
  - ImportSite.deferred = not module_level (includes type_only), as Python. A2 compares praxis.deferred vs (oracle.deferred or oracle.type_only).
  - tsc 6.0.2 default moduleResolution = Bundler (amd/umd/system: Classic). nodenext ESM mode does not resolve extensionless relative imports (measured).
  - recorder: file_edges extractor_version per source language (group by language).
  - modules: lang/typescript.py (glue), lang/ts_scan.py (lexer+sites), lang/ts_roots.py (config+resolve).
- 3. Built src/praxis/lang/{ts_scan,ts_config,ts_resolve,typescript}.py; registered TYPESCRIPT in lang/__init__ (BLIND += jsdoc); extract._readers passes [*present,*mine,*unclaimed] to roots, python.roots filters .py (same list as before). Smoke: ky 182 edges, zod 934, express 153 (= prototype). Sites vs prototype oracle json: 311/311, 1464/1464, 400/400, flags agree.
  dev scripts: ts1/dev/sitecmp.py, ts1/dev/reasons.py
- 4. Next: E13 site oracle experiments/E13/ts_sites.js + ts_accept.py.
- 5. E13: experiments/E13/ts_sites.js (site oracle, tsserver reference fallback, generated nodenext config for files with no tsconfig) + experiments/E13/ts_accept.py (4.4 JSON, A1-A5). Dev clones in ts1/repos (ky npm, zustand pnpm, immer yarn, zod hand links (lockfile broken), express/passport none). Dev results in ts1/dev/out/*.json: all A1-A5 true; zod 667/667 product edges, 1464/1464 all sites.
  Stress corpus ts1/stress (synthetic, git repo): fixed arrow-ASI reset, type-literal return annotation, `type X = import()`, generic `<T = U,>`/`<const T>`. Remaining accepted diffs: value-position `typeof import()`, json (out_of_construct), outDir map (oracle_env).
  dev scripts: ts1/dev/allsites.py <repo> (oracle vs x all sites)
- 6. Split lexer: lang/ts_lex.py (pass 1), ts_scan.py (pass 2). ruff/mypy/complexipy/C901 clean on lang. Dev regression all sites: ky 311, zod 1464, express 400, zustand 165, immer 111 (2 json diffs), passport 81: 0 diffs.
- 7. Judge: ts1/judge/judge2.py {py-only, as-is, py-files}. py-only: praxis/telltale/systemap byte-identical to step0/before. as-is: praxis, telltale identical; systemap differs ONLY by blind_spots/type_only key "docs": 0 (docs/assets/site.js now parsed) and extractor_version "x1-ts-v1+x1-v2"; edges identical. py-files (TS registered, Python files only): whole graphs identical on all three.
- 8. extract.StaticGraph.file_classes (whole scans; lang.file_class: registered name/unmapped_language/non_code/unknown); pool: unmapped_language partial note + separate report line, "Product files by language" block, POLYGLOT_ASSUMPTIONS when a non-Python reader claims a file, "no parsed source file" wording; recorder: file_edges split per language version (_by_version). Tests updated: test_pool (3 strings), test_extract bare-repo version x1-ts-v1+x1-v2. Suite: all pass after updates.
  ts_sites.js moved into experiments/E13/ts_sites.py as SCRIPT string (a .js file in praxis made praxis's own X1 two-language: version join changed).
  judge2 rerun with file_classes kept apart: py-only identical x3; as-is praxis/telltale identical, systemap differs only docs:0 keys + version join; py-files identical x3. No unmapped-language files in the 3.
- 9. Import cost (ts1/importcost/*.txt): Python-only scan loads praxis.lang + praxis.lang.python only; TS scan loads ts_* (cumulative 29 ms, ts_lex 17.8 ms: regex compile).
- 10. tests/integration/test_typescript.py (6 tests, binary-driven freeze + Stop hooks, privacy canaries, PYTHONPROFILEIMPORTTIME import cost, reader forms, bundle cut). Hand expectation corrected once before passing: component id of src/gen.py is "src/gen" (my model error, not a code change). btf: ts1/btf/btf.py, 9 faults, each test fails under >=1 (logs ts1/btf/*.log).
- 11. Pre-freeze fixes from synthetic stress only: IDENT via \w (master regex compile 9.3->0.8 ms; ts_lex import 17.8->3.5 ms), `for await (` is control, `{` after `<`/`,` in a return type is an object type, tsc arbitrary-extension lookup (./a.css -> a.d.css.ts, measured in oracle), chr guard in unescape. Stress agrees with oracle except the 3 accepted classes; solution-style references fallback + baseUrl agree.
- 12. FREEZE 2026-10-25T14:42:15Z. sha256 (also in ts1/freeze.sha256; verify with sha256sum -c before the held-out run):
    66a2dc901e96110df846549efb6641c15a2e1f95ad33c0e0a4b2ca4925a3904e  src/praxis/lang/__init__.py
    b4b248bedce0d1b8be01f3b23488587edcc3456e4985f0953da075796d7dbb18  src/praxis/lang/python.py
    094747cae47636d6c44008a0a175c87300518e2334d3ad7b60982b749574c9f9  src/praxis/lang/ts_config.py
    a4c28b95c01a33859f05f325f10402b89caedb084248eace754380fa9de2f2ec  src/praxis/lang/ts_lex.py
    7d90996be5e9dc347046c89d9fe2e30bb2d50f0e2a80d5ac18648156d1034cc2  src/praxis/lang/ts_resolve.py
    a4c46e193222454695d35e66eb3508a7c624e00058d8e3fcf2fdd5017ff36e98  src/praxis/lang/ts_scan.py
    d94c88366fce42632e8ae2a759f0b0d9c0faee7a63678a5a2890fab07fc2a032  src/praxis/lang/typescript.py
    c62485fa2d4d9aabe8029be725e2a3f85e60efa40f47a253ece5ffa4728458a9  experiments/E13/ts_accept.py
    97b558c141a52fad4c1f54d2c54c492a3b666d8a1ceff9df62f5239ed5d5c770  experiments/E13/ts_sites.py
    8ace31e589f0ca86adb5fa526338adf61332a838a7a8c8d2dcd7c38a2a9742e0  experiments/E13/shapes.py
    32a96e4a2964d1832c334e4863d61f9966e5bac8fafc4676b028806fbe89f1d0  src/praxis/extract.py
    80012550fcc15426d705e21e29c244740d7bd36c96c628078eec2692f1536df9  src/praxis/components.py
- 13. Held-out clones (--depth 1) in ts1/heldout/: webpack 669012dfc912167fa3b13cff233f8f0fa9755aef, vite 82e7aa0374a15e1c67594557ab5893c91578f897, excalidraw b7de45e88111f5680ae4bb3c3f3541c7d3b279fe, trpc 8d09833c76791b2ea7270af174fdab9f1408aeb8, date-fns a9739fb3e5384a810e90d7376446f6d84ecc5029. NOT yet extractor-run. Next: install deps (lockfile tool, scripts off), oracle-only A1-minimum check.
- 14. FREEZE ADDENDUM 2026-10-25T14:44:46Z, before any held-out extractor run: experiments/E13/ts_stop_cost.py (A4 clause 3, E04 rerun, largest product TS file, 20 sessions) 438c1c37cf9c0e3ad627e3be54816397c6f20421a9af7682c5b7a2bdad093d43. Tried on zod (dev) n=5: added p95 45.7 ms. lang/ and ts_accept/ts_sites unchanged since 12.
- 15. Installs: webpack, excalidraw `yarn install --frozen-lockfile --ignore-scripts` (yarn 1.22.22) exit 0; vite, trpc, date-fns `pnpm install --frozen-lockfile --ignore-scripts` (pnpm 10.28.0; vite/trpc declare pnpm 12) exit 0. Oracle-only check (ts1/heldout/oracle_check.py, no extractor): excalidraw 2821 product edges (3.3 s), webpack 3356 (13.5 s, 573 parse diagnostics over 17617 files, mostly test cases). Both >= 200.
  vite 1560 product edges, trpc 670, date-fns 5162: all five meet A1's minimum; oracle runs on all; no replacement.
- 16. Acceptance run (once): ts1/heldout/run_all.sh -> ts1/heldout/results/{name}.json, .log, {name}.stopcost.json. Verify hashes first.
- 17. RESULT webpack: FAIL (A4 p95 11.47 ms > 10; A5 7 too_large parse failures on oracle-parsed files). A1 P 1.0 R 0.9934 (3334/3356, 22 extract_miss), A2 1.0/1.0 (3922 matched), A3 3/3. DO NOT change the extractor.
- 18. RESULTS vite FAIL (A1 precision 0.9576, A3 0/1); excalidraw FAIL (A3 precision 0.8, A5 2 too_large); trpc PASS; date-fns see log. Stop-cost runs in progress.
- 19. date-fns PASS (P 0.9998, R 1.0, 1 oracle_env). Diagnosis (no code change):
  vite: 65 extractor-side resolve_wrong = pnpm `file:` deps installed as copies in node_modules/.pnpm (oracle realpath outside repo; x1 resolves name to in-repo package) -> env artefact but frozen classifier says resolve_wrong; 2 oracle-side resolve_wrong = committed symlinks (R23: ssr-wasm/src/imports.js 120000; preserve-symlinks); 2 resolve_extra = exports pattern target without extension (`./types/*` -> x1 adds .d.ts, tsc does not); 1 resolve_miss = tsc adds extensions to full name before exact (`./test.tsx` -> test.tsx.js); 2 oracle_env. A3: 1 extra component edge (event.d.ts -> packages/vite types).
  excalidraw: 1 oracle_env (dev-docs -> packages/excalidraw/index.tsx, no node_modules link) gives A3 4/5=0.8; A5 two *-wasm.ts > 512 KB.
  webpack: 22 extract_miss all from 4 too_large product files; A4 p95 11.47 (largest: schemas/WebpackOptions.check.js 421 KB 246 ms).
