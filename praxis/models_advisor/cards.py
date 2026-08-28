"""Model card schema, storage, and built-in cards.

A model card captures what a specific AI model is good and bad at, plus
how to prompt it well and what it costs. The system uses these cards
to give per-model coaching based on the user's actual usage.

Cards are versioned JSON. The built-in set ships with this package
under `praxis/data/builtin_cards/*.json`. Users can add or
override cards by dropping JSON files in:

  ~/.praxis/model_cards/

User-supplied cards override built-in cards with the same id.

Pricing values are documented in each card with a `pricing_source` URL
and `pricing_last_verified` date so you can re-check vendor pages
before relying on dashboard cost estimates.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from praxis.storage.profile_store import resolve_home


@dataclass
class ModelCard:
    """One model's profile."""

    id: str  # canonical: e.g. "claude-opus-4-7"
    family: str  # "claude" | "gpt" | "gemini" | "copilot" | ...
    display_name: str
    vendor: str
    tier: str  # "frontier" | "balanced" | "fast" | "specialized"
    context_window_tokens: int
    strengths: list[str]  # tasks this model excels at
    weaknesses: list[str]  # known failure modes
    prompting_quirks: list[str]  # vendor-specific best practices
    best_for: list[str]  # use cases where this model is the right pick
    avoid_for: list[str]  # use cases where another model is better
    notes: str  # free-form context
    sources: list[str] = field(default_factory=list)  # citations (URLs preferred)
    aliases: list[str] = field(default_factory=list)  # alt names found in session metadata
    version: str = "1.0"

    # Pricing: populated where the vendor publishes per-token rates.
    # None on subscription-only products (e.g. Copilot).
    input_per_million_usd: float | None = None
    output_per_million_usd: float | None = None
    pricing_last_verified: str | None = None  # ISO date string, e.g. "2026-01-15"
    pricing_source: str | None = None  # URL to the vendor pricing page
    pricing_notes: str | None = None  # caveats (e.g. tiered context-window pricing)


def _builtin_cards_dir() -> Path:
    """Path to the shipped JSON cards inside the installed package."""
    return Path(str(resources.files("praxis").joinpath("data/builtin_cards")))


def _load_card_file(path: Path) -> ModelCard | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ModelCard(**data)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        print(f"[model_cards] skipping invalid card {path.name}: {exc!r}", file=sys.stderr)
        return None


_BUILTIN_CACHE: list[ModelCard] | None = None


def builtin_cards() -> list[ModelCard]:
    """Read JSON-shipped built-in cards once, then return the cached list."""
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is None:
        cards: list[ModelCard] = []
        cards_dir = _builtin_cards_dir()
        if cards_dir.exists():
            for path in sorted(cards_dir.glob("*.json")):
                card = _load_card_file(path)
                if card is not None:
                    cards.append(card)
        _BUILTIN_CACHE = cards
    return list(_BUILTIN_CACHE)


# Built-in cards are shipped as JSON files under data/builtin_cards/ and
# loaded via builtin_cards(). The BUILTIN_CARDS module-level alias is kept
# for back-compat with code (and tests) that imports it directly.


class _BuiltinCardsList(list):
    """list[ModelCard] that lazy-loads from JSON files on first access."""

    def _ensure(self) -> None:
        if not super().__len__():
            self.extend(builtin_cards())

    def __iter__(self):
        self._ensure()
        return super().__iter__()

    def __len__(self) -> int:
        self._ensure()
        return super().__len__()

    def __getitem__(self, idx):
        self._ensure()
        return super().__getitem__(idx)


BUILTIN_CARDS: list[ModelCard] = _BuiltinCardsList()


def _normalize_model_string(s: str) -> str:
    return re.sub(r"[\s\.]+", "-", s.strip().lower())


def _user_cards_dir() -> Path:
    p = resolve_home() / "model_cards"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_all_cards() -> dict[str, ModelCard]:
    """Returns id -> ModelCard. User cards override built-ins."""
    cards: dict[str, ModelCard] = {c.id: c for c in builtin_cards()}
    user_dir = _user_cards_dir()
    for path in user_dir.glob("*.json"):
        card = _load_card_file(path)
        if card is not None:
            cards[card.id] = card
    return cards


def find_card_for_model_hint(model_hint: str | None) -> ModelCard | None:
    """Resolve a session's model_hint to a known card via id or alias.

    Strategy, in order:
      1. Direct id match against normalized hint.
      2. Alias match (any alias of any card, normalized).
      3. Prefix match: the user's hint *starts with* a known alias plus a
         separator. Used for dated suffixes like "claude-opus-4-7-20260315".

    Note on prefix matching: the match is one-way only. The hint must be
    longer than (or equal to) the alias. We do NOT match a short hint to
    a longer alias: that would silently route "claude" to whichever
    Claude card happens to sort first, which is worse than returning None.
    """
    if not model_hint:
        return None
    norm = _normalize_model_string(model_hint)
    cards = load_all_cards()

    # 1) Direct id match.
    if norm in cards:
        return cards[norm]

    # 2) Alias match.
    for card in cards.values():
        for alias in card.aliases:
            if _normalize_model_string(alias) == norm:
                return card

    # 3) Prefix match. Hint may be longer (dated suffix). Require a separator
    # after the alias to avoid matching "claude-haiku-4-5" against "claude".
    for card in cards.values():
        for cand in [card.id, *card.aliases]:
            cand_norm = _normalize_model_string(cand)
            if cand_norm and norm.startswith(cand_norm + "-"):
                return card

    return None


def export_card(card: ModelCard) -> dict:
    return asdict(card)
