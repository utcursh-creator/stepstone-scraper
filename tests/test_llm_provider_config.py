"""Settings resolution for the configurable LLM provider.

The scraper's evaluator defaults to OpenRouter + Claude Haiku but can be pointed
at any OpenAI-compatible provider (e.g. the client's own OpenAI account on their
instance) via LLM_BASE_URL + LLM_MODEL + LLM_API_KEY. OPENROUTER_API_KEY stays
accepted as an alias so existing deployments need no env change.
"""
import pytest
from models.config import Settings

_BASE = dict(
    PROXY_HOST="p", PROXY_USER="u", PROXY_PASS="x",
    STEPSTONE_EMAIL_1="a@b.c", STEPSTONE_PASS_1="p",
    AIRTABLE_PAT="pat", AIRTABLE_BASE_ID="app",
    AIRTABLE_CANDIDATES_TABLE="t", AIRTABLE_CREDIT_TABLE="t2",
    N8N_WEBHOOK_URL="https://x/wh",
)


def _env(monkeypatch, **extra):
    # Clear the two key vars so a stray real env can't leak into the assertions,
    # then set exactly what the test wants.
    for k in ("LLM_API_KEY", "OPENROUTER_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**_BASE, **extra}.items():
        monkeypatch.setenv(k, v)


def test_openrouter_api_key_is_accepted_as_alias(monkeypatch):
    """Backward compatibility: an existing deployment that only sets
    OPENROUTER_API_KEY keeps working unchanged."""
    _env(monkeypatch, OPENROUTER_API_KEY="or-key")
    s = Settings()
    assert s.llm_api_key == "or-key"


def test_defaults_are_openrouter_and_claude(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="k")
    s = Settings()
    assert s.llm_base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert s.llm_model == "anthropic/claude-haiku-4-5"


def test_llm_api_key_takes_precedence_over_the_alias(monkeypatch):
    """If both are set, the explicit LLM_API_KEY wins — so a client migrating to
    their own OpenAI key can set LLM_API_KEY without first deleting the old one."""
    _env(monkeypatch, OPENROUTER_API_KEY="or-key", LLM_API_KEY="openai-key")
    s = Settings()
    assert s.llm_api_key == "openai-key"


def test_full_openai_switch(monkeypatch):
    _env(
        monkeypatch,
        LLM_API_KEY="sk-openai",
        LLM_BASE_URL="https://api.openai.com/v1/chat/completions",
        LLM_MODEL="gpt-4o-mini",
    )
    s = Settings()
    assert s.llm_api_key == "sk-openai"
    assert s.llm_base_url == "https://api.openai.com/v1/chat/completions"
    assert s.llm_model == "gpt-4o-mini"


def test_missing_key_fails_fast(monkeypatch):
    """Neither LLM_API_KEY nor OPENROUTER_API_KEY set → boot fails clearly,
    rather than silently starting with no evaluator credential."""
    _env(monkeypatch)  # no key at all
    with pytest.raises(Exception):
        Settings()
