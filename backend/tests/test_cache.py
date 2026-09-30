"""The fallback cache serves only real earlier answers, flags them, and never crosses scopes."""
import pytest

from app import llm
from app.answer import cache_key
from app.tenants.admin import admin_tenant_conn

REFUND = "What's our refund window for enterprise customers?"


@pytest.mark.llm
def test_cache_fallback_is_real_flagged_and_scoped(client, as_user, monkeypatch):
    def post(user, question):
        return client.post("/api/ask", json={"question": question}, headers=as_user(user))

    live = post("marcus", REFUND).json()
    assert live["cached"] is False and live["refused"] is False

    def outage(*args, **kwargs):
        raise llm.Unavailable("simulated outage")

    monkeypatch.setattr(llm, "structured", outage)

    cached = post("marcus", REFUND).json()
    assert cached["cached"] is True and cached["cached_at"]
    assert cached["answer"] == live["answer"]

    # Priya must never receive Marcus's cached answer: she gets her own, or an error.
    r = post("priya", REFUND)
    if r.status_code == 200:
        body = r.json()
        assert all(c["scope"] != "finance" for c in body["citations"])
        assert "45" not in body["answer"]
    else:
        assert r.status_code == 503

    # With no earlier answer to fall back on, the result is a clear error.
    question = "What is the hotel cap for London?"
    with admin_tenant_conn("brindlewood") as conn:
        conn.execute("DELETE FROM answer_cache WHERE key = %s",
                     (cache_key(question, ["company-wide", "operations"]),))
    r = post("priya", question)
    assert r.status_code == 503
    assert "could not be reached" in r.json()["detail"]
