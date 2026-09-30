"""The demo's X-User-Id header must not exist in any code path. Second line of
defence only: the first is that nothing reads it (see test_tenancy's hint tests)."""
from pathlib import Path

REPO = Path("/repo")
CODE = ["backend/app", "web/src", "web/index.html", "web/vite.config.ts", "preflight.sh",
        "docker-compose.yml"]


def test_x_user_id_is_gone_from_all_code():
    assert (REPO / "backend/app").is_dir(), "run in the tools container (repo mounted at /repo)"
    hits = []
    for entry in CODE:
        root = REPO / entry
        for path in [root] if root.is_file() else root.rglob("*"):
            if path.is_file() and "node_modules" not in path.parts:
                text = path.read_text(errors="ignore").lower()
                if "x-user-id" in text or "x_user_id" in text:
                    hits.append(str(path.relative_to(REPO)))
    assert hits == []
