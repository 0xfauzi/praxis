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
_GITHUB_PAT = re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})\b")

# JWTs: three base64url segments separated by dots. We require the
# leading `eyJ` (which is base64 for the `{"` that starts every JWT
# header) so we don't redact random dotted identifiers.
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

# Google / Gemini API keys: literal `AIza` + 35 url-safe chars. These
# appear bare (not behind a "key" label) in Gemini code and URLs, so the
# generic labeled pattern would miss them.
_GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")

# Slack tokens: bot/user/app/refresh/legacy, `xox[baprs]-` + a dash-joined
# high-entropy tail.
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")

# PEM private-key blocks (RSA / EC / OPENSSH / DSA / generic). The body is
# matched lazily so it stops at the first END line and cannot catastrophically
# backtrack. A leaked private key block is among the worst things to ship to a
# third-party LLM, so the whole armored block is replaced.
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
)

# Labeled secret tails: a >= 20 char [A-Za-z0-9_-] sequence that
# follows the literal word "key", "token", "secret", or "password"
# (case-insensitive). `\b...\b` keeps us from matching the literal
# inside a larger identifier like `keyboard`. `\W*` allows the
# common separators (`=`, `:`, space, quotes, ...) between label
# and value while still requiring the high-entropy tail to start
# close to the label -- intervening English words like "is" break
# `\W*` and so disqualify the match. The value class deliberately
# excludes `[` and `]`, so an existing `[REDACTED]` marker can't
# satisfy the {20,} length and a second pass is a no-op.
_GENERIC_LABELED_SECRET = re.compile(
    r"(?i)(\b(?:key|token|secret|password)\b\W*)[A-Za-z0-9_\-]{20,}"
)


# Each entry is (pattern, replacement) for re.sub. Provider patterns
# redact the whole match; the labeled-secret pattern uses \1 to keep
# the label + separator and redact only the high-entropy tail.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM blocks first: redact the whole armored block before any other
    # pattern can match a fragment inside it.
    (_PEM_PRIVATE_KEY, PLACEHOLDER),
    (_ANTHROPIC, PLACEHOLDER),
    (_OPENAI, PLACEHOLDER),
    (_GOOGLE_API_KEY, PLACEHOLDER),
    (_SLACK_TOKEN, PLACEHOLDER),
    (_AWS_ACCESS_KEY, PLACEHOLDER),
    (_GITHUB_PAT, PLACEHOLDER),
    (_JWT, PLACEHOLDER),
    # Generic labeled secret runs last: it only redacts a high-entropy tail
    # that follows a key/token/secret/password label, so the specific
    # provider patterns above get first claim on their own formats.
    (_GENERIC_LABELED_SECRET, r"\1" + PLACEHOLDER),
)


def redact_secrets(text: str) -> str:
    """Replace known secret formats with [REDACTED].

    Covers Anthropic API keys, OpenAI API keys, AWS access key IDs,
    GitHub personal access tokens, JWT-shaped tokens, and any >= 20
    character alnum/underscore/hyphen tail that follows a "key",
    "token", "secret", or "password" label. Safe to call on
    already-redacted strings.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
