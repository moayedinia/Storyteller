"""Shared fixtures. The app binds its SQLite path at import time, so the environment is
pointed at a throwaway directory before anything from `app` is imported."""
import os
import tempfile
import uuid

_TMP = tempfile.mkdtemp(prefix="storyteller-tests-")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["DATA_DIR"] = os.path.join(_TMP, "novels")
os.environ["STORYTELLER_ADMIN_PASSWORD"] = "admin-test-password"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["LLM_API_KEY"] = ""
os.environ["QLOO_ENABLED"] = "false"
# Pin the allowance the assertions below hardcode.
os.environ["TRIAL_MAX_EPISODES"] = "3"
os.environ["TRIAL_MAX_WORDS_PER_EPISODE"] = "1500"
os.environ.pop("STORYTELLER_TEST_MODE", None)

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.blueprint import StoryBlueprint
from app.models.episode import EpisodeResponse
from app.storage.store import store

TEST_PASSWORD = "test-password-123"


class Account:
    def __init__(self, user: dict, token: str):
        self.id = user["id"]
        self.email = user["email"]
        self.username = user["username"]
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def client():
    """Cookie-free client: every request authenticates explicitly through headers."""
    with TestClient(app) as c:
        yield c


def _cookie_free(client: TestClient, method: str, url: str, **kwargs):
    res = client.request(method, url, **kwargs)
    client.cookies.clear()
    return res


@pytest.fixture
def make_user(client):
    def _make() -> Account:
        name = f"author_{uuid.uuid4().hex[:10]}"
        res = _cookie_free(client, "POST", "/api/auth/register", json={
            "email": f"{name}@example.com", "username": name, "password": TEST_PASSWORD, "full_name": "Test Author"
        })
        assert res.status_code == 200, res.text
        body = res.json()
        return Account(body["user"], body["token"])
    return _make


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture(scope="session")
def admin(client):
    res = _cookie_free(client, "POST", "/api/auth/login", json={"login": "admin", "password": "admin-test-password"})
    assert res.status_code == 200, res.text
    body = res.json()
    return Account(body["user"], body["token"])


def novel_payload(**overrides) -> dict:
    payload = {"title": "Trial Story", "premise": "A test premise.", "total_episodes": 3, "words_per_episode": 1200, "start_now": False}
    payload.update(overrides)
    return payload


def save_legacy_novel(owner_id: str, total_episodes: int = 5) -> str:
    """A novel owned by the account but created outside its trial (as if it predated the feature)."""
    novel_id = f"novel_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    store.save_blueprint(StoryBlueprint(
        novel_id=novel_id, title="Legacy Story", total_episodes=total_episodes,
        owner_id=owner_id, owner_name="Test Author", created_at_utc=now, updated_at_utc=now
    ))
    return novel_id


def save_episodes(novel_id: str, count: int) -> None:
    for n in range(1, count + 1):
        store.save_episode(EpisodeResponse(
            novel_id=novel_id, episode_number=n, title=f"Chapter {n}", pov_character="Narrator",
            summary="Summary.", content="Some prose.", word_count=2
        ))
