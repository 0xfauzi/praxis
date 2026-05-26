"""Tests for praxis.redactor.

Each provider regex is exercised with a representative key, plus
checks that surrounding prose is preserved.
"""
from __future__ import annotations

from praxis.redactor import PLACEHOLDER, redact_secrets


def test_anthropic_key_redacted():
    key = "sk-ant-api03-FAKEFAKEfake_-1234567890ABCDEFGHIJabcdefghij"
    out = redact_secrets(f"my key is {key} please keep it secret")
    assert key not in out
    assert PLACEHOLDER in out
    assert out.startswith("my key is ")
    assert out.endswith(" please keep it secret")


def test_openai_key_redacted():
    key = "sk-proj-FAKEopenai1234567890ABCDEFabcdef_-XYZ0987654321"
    out = redact_secrets(f"OPENAI_API_KEY={key}")
    assert key not in out
    assert PLACEHOLDER in out


def test_openai_legacy_key_redacted():
    key = "sk-FAKE1234567890abcdefABCDEFghijKLMN"
    out = redact_secrets(f"export OPENAI_API_KEY={key}")
    assert key not in out
    assert PLACEHOLDER in out


def test_aws_access_key_redacted():
    # AKIAIOSFODNN7EXAMPLE is AWS's documented dummy access key.
    key = "AKIAIOSFODNN7EXAMPLE"
    out = redact_secrets(f"AWS_ACCESS_KEY_ID = {key}")
    assert key not in out
    assert PLACEHOLDER in out


def test_github_pat_redacted():
    # Classic PAT format: `ghp_` + exactly 36 alphanumerics.
    key = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    out = redact_secrets(f"git remote add origin https://{key}@github.com/me/repo")
    assert key not in out
    assert PLACEHOLDER in out


def test_github_fine_grained_pat_redacted():
    # github_pat_ + 82 chars
    key = "github_pat_" + ("A" * 22) + "_" + ("B" * 59)
    out = redact_secrets(f"token={key}")
    assert key not in out
    assert PLACEHOLDER in out


def test_jwt_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = redact_secrets(f"Authorization: Bearer {jwt}")
    assert jwt not in out
    assert PLACEHOLDER in out


def test_anthropic_and_openai_do_not_collide():
    anthropic = "sk-ant-api03-FAKEfake1234567890abcdefghij_-ABCDEFGHIJ"
    openai = "sk-proj-FAKEopenai1234567890ABCDEFabcdef_-XYZ09876"
    out = redact_secrets(f"a={anthropic} o={openai}")
    assert anthropic not in out
    assert openai not in out
    # Two distinct redactions should have happened.
    assert out.count(PLACEHOLDER) == 2


def test_prose_without_secrets_is_unchanged():
    text = "Please use a key, token, secret, or password to authenticate."
    assert redact_secrets(text) == text
