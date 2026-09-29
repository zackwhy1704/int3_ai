"""Questions with no supporting source never produce an answer."""
from .conftest import USERS


def test_unsupported_questions_refuse(client, questions):
    for user in USERS:
        for question in questions["unsupported"]:
            r = client.post("/api/ask", json={"question": question}, headers={"X-User-Id": user})
            assert r.status_code == 200
            body = r.json()
            assert body["refused"] is True, (user, question, body)
            assert "answer" not in body
            assert body["message"] == "No reliable source found"
