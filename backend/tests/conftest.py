import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app

SEED_DIR = Path(os.environ.get("SEED_DIR", "/seed"))
USERS = ["priya", "marcus", "ada"]


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def questions():
    return yaml.safe_load((SEED_DIR / "questions.yaml").read_text())


@pytest.fixture(scope="session")
def memberships():
    return yaml.safe_load((SEED_DIR / "users.yaml").read_text())["memberships"]
