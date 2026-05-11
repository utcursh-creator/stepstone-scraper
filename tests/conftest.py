"""Shared pytest fixtures + import-time env stubs.

main.py imports settings = Settings() at module load, which fails without
required env vars (proxy/airtable/recruitee/openrouter). For unit tests we
don't actually hit those services — set safe placeholders before any test
file imports main.
"""
import os

# Set before any test module imports main / Settings — placeholders only,
# real values come from .env in production.
os.environ.setdefault("PROXY_HOST", "proxy.test")
os.environ.setdefault("PROXY_PORT", "12321")
os.environ.setdefault("PROXY_USER", "user")
os.environ.setdefault("PROXY_PASS", "pass")
os.environ.setdefault("STEPSTONE_EMAIL_1", "test@example.com")
os.environ.setdefault("STEPSTONE_PASS_1", "test_password")
os.environ.setdefault("OPENROUTER_API_KEY", "or-test")
os.environ.setdefault("AIRTABLE_PAT", "pat_test")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")
os.environ.setdefault("AIRTABLE_CANDIDATES_TABLE", "tbl_test")
os.environ.setdefault("AIRTABLE_CREDIT_TABLE", "tbl_test_credit")
os.environ.setdefault("N8N_WEBHOOK_URL", "https://example.test/webhook")
os.environ.setdefault("RECRUITEE_API_TOKEN", "recruitee_test_token")
os.environ.setdefault("RECRUITEE_COMPANY_ID", "61932")
