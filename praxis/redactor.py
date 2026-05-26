"""Secret redaction.

Replaces known API key / token formats with [REDACTED] so that user
transcripts pasted into moments can be safely persisted and later
re-rendered into the weekly HTML digest the user opens in a browser.

Spec section 4.4 requires this to run BEFORE any moment is persisted
or rendered. Running it more than once is fine; running it zero
times is not.
"""
from __future__ import annotations

import re

PLACEHOLDER = "[REDACTED]"


# Anthropic API keys: `sk-ant-...` followed by a long base64url-ish
# tail. Real keys are 90+ chars after the prefix; we accept >= 20 so
# representative fixtures and shortened examples are still caught.
_ANTHROPIC = re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")

# OpenAI keys: `sk-...`, `sk-proj-...`, `sk-svcacct-...`. The negative
# lookahead avoids stealing Anthropic matches (which start with sk-ant-).
_OPENAI = re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}")

# AWS access key IDs are exactly 20 chars: a 4-char prefix (AKIA for
# long-lived users, ASIA for STS temporary, AROA for roles, AIDA for
# IAM users) followed by 16 uppercase alphanumerics.
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[0-9A-Z]{16}\b")

# GitHub personal access tokens.
#   Classic / OAuth / user / server / refresh: `gh[oprsu]_` + 36 chars
#   Fine-grained: `github_pat_` + 82 chars (with underscores allowed)
_GITHUB_PAT = re.compile(
    r"\b(?:gh[oprsu]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})\b"
)

# JWTs: three base64url segments separated by dots. We require the
# leading `eyJ` (which is base64 for the `{"` that starts every JWT
# header) so we don't redact random dotted identifiers.
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


_PROVIDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    _ANTHROPIC,
    _OPENAI,
    _AWS_ACCESS_KEY,
    _GITHUB_PAT,
    _JWT,
)


def redact_secrets(text: str) -> str:
    """Replace known secret formats with [REDACTED].

    Covers Anthropic API keys, OpenAI API keys, AWS access key IDs,
    GitHub personal access tokens, and JWT-shaped tokens. Safe to
    call on already-redacted strings.
    """
    for pattern in _PROVIDER_PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    return text
