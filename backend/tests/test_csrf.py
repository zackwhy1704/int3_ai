"""State-changing requests need the session's CSRF token and the app's own origin."""
from app import config

ASK = {"question": "What is the hotel cap for London?"}


def test_ask_without_csrf_token_is_403(client, as_user):
    h = {k: v for k, v in as_user("priya").items() if k != "X-CSRF-Token"}
    r = client.post("/api/ask", json=ASK, headers=h)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]


def test_ask_with_another_sessions_csrf_token_is_403(client, as_user):
    h = {**as_user("priya"), "X-CSRF-Token": as_user("marcus")["X-CSRF-Token"]}
    assert client.post("/api/ask", json=ASK, headers=h).status_code == 403


def test_ask_from_a_foreign_origin_is_403(client, as_user):
    h = {**as_user("priya"), "Origin": "https://evil.example"}
    assert client.post("/api/ask", json=ASK, headers=h).status_code == 403


def test_ask_without_origin_is_403(client, as_user):
    h = {k: v for k, v in as_user("priya").items() if k != "Origin"}
    assert client.post("/api/ask", json=ASK, headers=h).status_code == 403


def test_logout_without_csrf_token_is_403(client, as_user):
    h = {"Cookie": as_user("priya")["Cookie"], "Origin": config.APP_ORIGIN}
    assert client.post("/api/auth/logout", headers=h).status_code == 403
    assert client.get("/api/session", headers=h).status_code == 200
