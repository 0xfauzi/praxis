"""Tests for the v0.2 weekly digest HTML renderer.

US-061 acceptance criteria (spec section 13.1):
  - Generated HTML contains no external CSS, JS, font, or image references.
  - Double-clicking the file renders correctly in a default browser
    (smoke-checked here by asserting the output is a valid HTML document
    with inline CSS and no required network fetches).

These tests build a minimal ``WeeklyDigest`` by hand rather than going
through the full weekly pipeline, so they stay fast and pure.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

import pytest

from praxis.reports.digest_html import WeeklyDigest, render


def _digest(
    week_iso: str = "2026-W21",
    generated_at: datetime | None = None,
) -> WeeklyDigest:
    if generated_at is None:
        generated_at = datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)
    return WeeklyDigest(week_iso=week_iso, generated_at=generated_at)


# ------------------------------------------------------------ structural shape


def test_render_starts_with_doctype():
    """The output must be a full HTML document so a browser can open it
    directly via the file:// protocol."""
    out = render(_digest())
    assert out.startswith("<!doctype html>")


def test_render_closes_html_tag():
    """A well-formed document - rules out accidentally truncated output."""
    out = render(_digest()).rstrip()
    assert out.endswith("</html>")


def test_render_returns_str():
    assert isinstance(render(_digest()), str)


def test_render_includes_week_iso_in_masthead():
    """The masthead echoes the week_iso so the user knows which week they
    are reading without opening DevTools."""
    out = render(_digest(week_iso="2026-W21"))
    assert "2026-W21" in out


def test_render_includes_generated_date():
    """The masthead includes the generation date as a human-readable
    timestamp."""
    out = render(
        _digest(generated_at=datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc))
    )
    assert "May 27, 2026" in out


# ----------------------------------------------------------- self-containment


def test_render_has_no_link_tags():
    """No ``<link>`` element of any kind - that rules out external CSS,
    favicons, and webfont stylesheets in one assertion."""
    out = render(_digest()).lower()
    assert "<link" not in out


def test_render_has_no_script_tags():
    """No ``<script>`` tags. The digest is static reading material with
    no client-side behavior; allowing scripts would weaken the
    self-contained contract."""
    out = render(_digest()).lower()
    assert "<script" not in out


def test_render_has_no_img_tags():
    """No ``<img>`` tags. If imagery is ever required it must be inlined
    as a ``data:`` URI, so this test guards the simple-by-default path."""
    out = render(_digest()).lower()
    assert "<img" not in out


def test_render_has_no_external_urls():
    """No ``http://`` or ``https://`` URLs anywhere - the file must open
    with no network access. If a future story needs to link to vendor
    documentation (e.g. an anchor in the colophon), this assertion can be
    relaxed in a targeted way."""
    out = render(_digest())
    assert "http://" not in out
    assert "https://" not in out


def test_render_has_no_protocol_relative_urls():
    """``//cdn.example.com/...`` would fetch via the page's protocol -
    still a network call, still banned."""
    out = render(_digest())
    # Avoid matching the doctype declaration or comments by scanning attribute
    # contexts where URLs commonly appear.
    for attr in ("src=", "href="):
        for match in re.finditer(rf'{attr}"([^"]*)"', out):
            value = match.group(1)
            assert not value.startswith("//"), (
                f"protocol-relative URL in {attr}{value!r}"
            )


def test_render_inlines_css_in_style_block():
    """All CSS must live in an inline ``<style>`` block."""
    out = render(_digest())
    assert "<style>" in out
    assert "</style>" in out


def test_render_has_no_css_import():
    """``@import url(...)`` would pull a remote stylesheet - banned."""
    out = render(_digest())
    assert "@import" not in out


def test_render_has_no_css_url_calls():
    """``url(...)`` in CSS commonly references external images or fonts.
    We don't need them for the digest; banning the whole construct keeps
    the file portable. If a future story needs ``url(data:...)`` it can
    relax this assertion to permit only the ``data:`` scheme."""
    out = render(_digest())
    assert "url(" not in out


def test_render_uses_system_font_stack():
    """Font stack must lead with a face that ships on every default
    browser, so the digest looks coherent without any webfont fetch."""
    out = render(_digest())
    assert "Georgia" in out


def test_render_charset_is_utf8():
    """UTF-8 is required for the digest to render the section glyphs
    and any non-ASCII content in user transcripts."""
    out = render(_digest())
    assert 'charset="utf-8"' in out


def test_render_has_viewport_meta():
    """Smoke check that the document is a regular responsive HTML page,
    not a fragment."""
    out = render(_digest())
    assert 'name="viewport"' in out


# ------------------------------------------------------------ dataclass shape


def test_weekly_digest_is_frozen():
    """``WeeklyDigest`` is immutable. Mutation would let renderers
    accidentally smuggle state and would make reuse across tests
    error-prone."""
    digest = _digest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.week_iso = "2026-W22"  # type: ignore[misc]


def test_weekly_digest_fields():
    """Lock the minimal shape of the input contract. Later stories add
    fields; this test will be updated when they do."""
    digest = _digest()
    assert digest.week_iso == "2026-W21"
    assert digest.generated_at == datetime(2026, 5, 27, 18, 0, tzinfo=timezone.utc)


def test_render_html_escapes_week_iso():
    """The week_iso is interpolated into the HTML; if a malformed value
    ever reaches the renderer it must not break the page."""
    out = render(_digest(week_iso="<W>"))
    # The literal angle bracket form must not appear as a tag.
    assert "<W>" not in out
    # The escaped form does.
    assert "&lt;W&gt;" in out
