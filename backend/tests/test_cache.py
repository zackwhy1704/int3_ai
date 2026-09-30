"""The fallback cache serves only real earlier answers, flags them, and never crosses scopes."""
import pytest

from app import llm
from app.answer import cache_key
from app.db import connect
from app.scopes import user_scopes

REFUND = "What's our refund window for enterprise customers?"


def post(client, user, question):
    return client.post("/api/ask", json={"question": question}, headers={"X-User-Id": user})


@pytest.mark.llm
def test_cache_fallback_is_real_flagged_and_scoped(client, monkeypatch):
    live = post(client, "marcus", REFUND).json()
    assert live["cached"] is False and live["refused"] is False

    def outage(*args, **kwargs):
        raise llm.Unavailable("simulated outage")

    monkeypatch.setattr(llm, "structured", outage)

    cached = post(client, "marcus", REFUND).json()
    assert cached["cached"] is True and cached["cached_at"]
    assert cached["answer"] == live["answer"]

    # Priya must never receive Marcus's cached answer: she gets her own, or an error.
    r = post(client, "priya", REFUND)
    if r.status_code == 200:
        body = r.json()
        assert all(c["scope"] != "finance" for c in body["citations"])
        assert "45" not in body["answer"]
    else:
        assert r.status_code == 503

    # With no earlier answer to fall back on, the result is a clear error.
    question = "What is the hotel cap for London?"
    with connect() as conn:
        conn.execute("DELETE FROM answer_cache WHERE key = %s",
                     (cache_key(question, user_scopes("priya")),))
    r = post(client, "priya", question)
    assert r.status_code == 503
    assert "could not be reached" in r.json()["detail"]
