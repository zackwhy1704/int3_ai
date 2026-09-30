"""Questions with no supporting source never produce an answer."""
from .conftest import A_USERS


def test_unsupported_questions_refuse(client, as_user, questions):
    for user in A_USERS:
        for question in questions["unsupported"]:
            r = client.post("/api/ask", json={"question": question}, headers=as_user(user))
            assert r.status_code == 200
            body = r.json()
            assert body["refused"] is True, (user, question, body)
            assert "answer" not in body
            assert body["message"] == "No reliable source found"
