"""Self-contained HTML weekly digest renderer (spec section 13.1).

The digest file is written to ``~/.praxis/weeks/<iso>.html`` and must
open by double-click in a default browser with no network access.
This module is the renderer; persistence (writing to disk, updating the
``latest.html`` symlink) is handled by a separate story.

Self-containment rules (US-061 acceptance):

* All CSS lives in an inline ``<style>`` block. No ``<link rel="stylesheet">``.
* No webfonts. The font stack lists system faces only (Georgia primary,
  with a sans-serif fallback for chrome). A later story (US-063)
  re-introduces Libre Baskerville via the v0.1 inlined-fallback path.
* No ``<script>`` tags - the digest is static reading material.
* No ``<img>`` tags. If imagery is ever required, it must be inlined as
  a ``data:`` URI so the file remains portable.
* No ``@import`` URLs in CSS.

The renderer is a pure function from a ``WeeklyDigest`` dataclass to an
HTML string. Later stories extend ``WeeklyDigest`` with the moment, cost
ledger, tasks, dim scores, follow-up, and "one thing to try" panels per
spec section 6.1; the self-containment contract enforced here will not
change as those fields are added.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WeeklyDigest:
    """Input contract for one weekly digest render.

    Minimal scaffolding for US-061. The masthead is the only section
    rendered at this stage; later stories add the trajectory headline,
    moment, cost ledger, tasks, dimensions, follow-up, and "one thing
    to try next week" panels per spec section 6.1.
    """

    week_iso: str
    generated_at: datetime


def render(digest: WeeklyDigest) -> str:
    """Render the weekly digest as a self-contained HTML document.

    The output meets spec section 13.1's "fully self-contained, opens by
    double-click" requirement: all CSS is inlined, and no external CSS,
    JS, fonts, or images are referenced.
    """
    week = html.escape(digest.week_iso)
    generated = html.escape(digest.generated_at.strftime("%B %d, %Y"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praxis - Week of {week}</title>
<style>
:root {{
  --primary: #C1573B;
  --cream-100: #FAF7F2;
  --ink-900: #1a1a1a;
  --ink-500: #6b6b6b;
  --border-light: rgba(26, 26, 26, 0.08);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  background: var(--cream-100);
  color: var(--ink-900);
  font-family: Georgia, serif;
  font-size: 16px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.page {{ max-width: 880px; margin: 0 auto; padding: 80px 32px 120px; }}
.masthead {{
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 32px;
  margin-bottom: 64px;
}}
.eyebrow {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--primary);
  font-weight: 500;
}}
.masthead-title {{
  font-family: Georgia, serif;
  font-size: 44px;
  line-height: 1.15;
  margin-top: 16px;
  letter-spacing: -0.01em;
}}
.masthead-meta {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--ink-500);
  margin-top: 24px;
}}
</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <div class="eyebrow">Praxis &middot; Weekly Read</div>
    <h1 class="masthead-title">Week of {week}</h1>
    <div class="masthead-meta">Generated {generated}</div>
  </header>
</main>
</body>
</html>"""
