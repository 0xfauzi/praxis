"""The TypeScript and JavaScript import reader (x1-ts-v1): sites read by a hand lexer
(``ts_scan``), resolved against roots built once per scan from the listing, every
tsconfig.json and every package.json (``ts_config``, ``ts_resolve``). Standard library
only, and loaded only when a scan holds a file with one of its suffixes.

Kinds: import, import_type, side_effect, export_from, export_type_from, import_equals,
require, dynamic_import, import_type_node, reference_path; and, for blind sites only,
resolve (``require.resolve``, ``import.meta.resolve``, ``new URL(s, import.meta.url)``),
context (``require.context``, ``import.meta.glob``) and jsdoc (an ``import()`` type in
a JSDoc comment). ``deferred`` is true for every site that does not run when the file is
loaded: ``import()``, ``require()`` inside a function body, ``import defer`` and every
type-only site.

Design: docs/design/04-languages.md sections 1 and 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from praxis.lang import TYPESCRIPT, ImportSite, Language, Resolved, ts_resolve
from praxis.lang.ts_scan import read

if TYPE_CHECKING:
    from pathlib import Path

    from praxis.lang.ts_resolve import Roots


class _TypeScript(Language["Roots"]):
    NAME = TYPESCRIPT.name
    VERSION = TYPESCRIPT.version
    SUFFIXES = TYPESCRIPT.suffixes
    KINDS = frozenset(
        {
            "import",
            "import_type",
            "side_effect",
            "export_from",
            "export_type_from",
            "import_equals",
            "require",
            "dynamic_import",
            "import_type_node",
            "reference_path",
            "resolve",
            "context",
            "jsdoc",
        }
    )
    REASONS = frozenset(
        {
            "relative",
            "alias",
            "base_url",
            "workspace",
            "workspace_outdir",
            "package_imports",
            "reference",
            "external",
            "asset",
            "dynamic",
            "relative_missing",
            "alias_unconfigured",
            "unresolved_local",
            "nul",
            "jsdoc",
        }
    )

    @override
    def files(self, repo_root: Path, listed: list[str]) -> list[str]:
        return [
            p for p in listed if p.endswith(self.SUFFIXES) and (repo_root / p).is_file()
        ]

    @override
    def roots(self, repo_root: Path, listed: list[str]) -> Roots:
        return ts_resolve.build(repo_root, listed)

    @override
    def imports(self, path: str, source: bytes) -> list[ImportSite]:
        return read(path, source)

    @override
    def resolve(self, site: ImportSite, importer: str, roots: Roots) -> Resolved:
        return ts_resolve.resolve(site, importer, roots)


LANGUAGE = _TypeScript()
