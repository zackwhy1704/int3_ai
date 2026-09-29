"""Cross-user permission tests. These must pass 100%."""
import pytest

from .conftest import USERS


def all_questions(q):
    return q["answerable"] + q["unsupported"]


def foreign_canaries(questions, memberships, user):
    return [c for scope, cs in questions["canaries"].items()
            if scope not in memberships[user] for c in cs]


def test_search_never_returns_out_of_scope_chunks(client, questions, memberships):
    for user in USERS:
        allowed = set(memberships[user])
        for question in all_questions(questions):
            r = client.get("/api/search", params={"q": question}, headers={"X-User-Id": user})
            assert r.status_code == 200
            for hit in r.json()["results"]:
                assert hit["scope"] in allowed, (user, question, hit)


def test_search_text_contains_no_foreign_canaries(client, questions, memberships):
    for user in USERS:
        canaries = foreign_canaries(questions, memberships, user)
        for question in all_questions(questions):
            r = client.get("/api/search", params={"q": question}, headers={"X-User-Id": user})
            blob = " ".join(h["text"] + " " + h["doc_title"] for h in r.json()["results"])
            for canary in canaries:
                assert canary not in blob, (user, question, canary)


@pytest.mark.llm
def test_answers_never_cite_or_contain_foreign_scopes(client, questions, memberships):
    for user in USERS:
        allowed = set(memberships[user])
        canaries = foreign_canaries(questions, memberships, user)
        for question in questions["answerable"]:
            r = client.post("/api/ask", json={"question": question}, headers={"X-User-Id": user})
            assert r.status_code == 200
            body = r.json()
            if body["refused"]:
                continue
            for c in body["citations"]:
                assert c["scope"] in allowed, (user, question, c)
            for canary in canaries:
                assert canary.lower() not in body["answer"].lower(), (user, question, canary)


def test_document_outside_scope_is_404(client):
    r = client.get("/api/documents/finance-annual-contract-terms", headers={"X-User-Id": "priya"})
    assert r.status_code == 404
    r = client.get("/api/documents/finance-annual-contract-terms", headers={"X-User-Id": "marcus"})
    assert r.status_code == 200


def test_brain_outside_membership_is_404(client):
    r = client.get("/api/search", params={"q": "refund", "brain_id": "finance"},
                   headers={"X-User-Id": "priya"})
    assert r.status_code == 404


def test_unknown_user_rejected(client):
    r = client.get("/api/search", params={"q": "refund"}, headers={"X-User-Id": "mallory"})
    assert r.status_code == 401
