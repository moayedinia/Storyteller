import asyncio
import threading
import uuid

from app.engine.pipeline import generate_single_episode, run_novel_episodes
from app.models.enums import NovelStatus
from app.storage.store import store
from app.storage.trial import trial_manager
from tests.conftest import novel_payload, save_episodes, save_legacy_novel


def trial_status(client, account) -> dict:
    res = client.get("/api/trial/status", headers=account.headers)
    assert res.status_code == 200, res.text
    return res.json()


def create_trial_novel(client, account, **overrides) -> str:
    res = client.post("/api/novels", json=novel_payload(**overrides), headers=account.headers)
    assert res.status_code == 200, res.text
    return res.json()["novel_id"]


def assert_upgrade_required(res, code: str) -> None:
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    assert detail["code"] == code
    assert detail["upgrade_path"] == "waitlist"
    assert "waitlist" in detail["message"].lower()


# ---------- eligibility ----------

def test_new_account_has_one_unused_trial(client, user):
    status = trial_status(client, user)
    assert status["plan"] == "trial"
    assert status["trial_available"] is True
    assert status["trial_used"] is False
    assert status["chapters_limit"] == 3
    assert status["chapters_remaining"] == 3
    assert status["max_words_per_chapter"] == 1500


def test_creating_a_novel_consumes_the_trial(client, user):
    novel_id = create_trial_novel(client, user)
    status = trial_status(client, user)
    assert status["trial_available"] is False
    assert status["trial_used"] is True
    assert status["trial_exhausted"] is False
    assert status["trial_novel_id"] == novel_id
    assert status["chapters_remaining"] == 3


def test_second_novel_is_rejected(client, user):
    create_trial_novel(client, user)
    res = client.post("/api/novels", json=novel_payload(title="Second"), headers=user.headers)
    assert_upgrade_required(res, "trial_used")
    assert len(client.get("/api/novels", headers=user.headers).json()) == 1


def test_deleting_the_trial_novel_does_not_restore_the_trial(client, user):
    novel_id = create_trial_novel(client, user)
    assert client.delete(f"/api/novels/{novel_id}", headers=user.headers).status_code == 200
    res = client.post("/api/novels", json=novel_payload(), headers=user.headers)
    assert_upgrade_required(res, "trial_used")
    status = trial_status(client, user)
    assert status["trial_exhausted"] is True
    assert status["trial_novel_exists"] is False
    assert status["chapters_remaining"] == 0


def test_trial_novel_cannot_exceed_chapter_allowance(client, user):
    res = client.post("/api/novels", json=novel_payload(total_episodes=4), headers=user.headers)
    assert_upgrade_required(res, "trial_limit")
    assert trial_status(client, user)["trial_available"] is True  # a rejected request does not burn the trial


def test_trial_novel_cannot_exceed_chapter_length_allowance(client, user):
    res = client.post("/api/novels", json=novel_payload(words_per_episode=1800), headers=user.headers)
    assert_upgrade_required(res, "trial_limit")
    assert trial_status(client, user)["trial_available"] is True


def test_omitted_sizes_default_to_the_trial_allowance(client, user):
    res = client.post("/api/novels", json={"title": "Defaults", "start_now": False}, headers=user.headers)
    assert res.status_code == 200, res.text
    assert res.json()["total_episodes"] == 3
    assert res.json()["words_per_episode"] == 1500


def test_admin_is_not_limited(client, admin):
    for _ in range(2):
        res = client.post("/api/novels", json=novel_payload(total_episodes=8, words_per_episode=2500), headers=admin.headers)
        assert res.status_code == 200, res.text
    assert trial_status(client, admin)["plan"] == "unlimited"


def test_concurrent_claims_grant_exactly_one_trial(user):
    results = []
    barrier = threading.Barrier(8)

    errors = []

    def claim():
        barrier.wait()
        try:
            results.append(trial_manager.claim_trial(user.id, f"novel_{uuid.uuid4().hex[:8]}"))
        except Exception as exc:  # pragma: no cover - surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_trial_endpoints_require_authentication(client):
    assert client.get("/api/trial/status").status_code == 401
    assert client.post("/api/novels", json=novel_payload()).status_code == 401


# ---------- limit enforcement ----------

def test_trial_novel_can_generate_within_allowance(client, user):
    """Chapter 1 of the trial novel is accepted, not refused as an upgrade case.

    No model is configured in tests, so the accepted request is the 'queued' response;
    what matters here is that trial enforcement does not block it.
    """
    novel_id = create_trial_novel(client, user)
    res = client.post(f"/api/novels/{novel_id}/write-next", headers=user.headers)
    assert res.status_code == 200, res.text
    assert res.json().get("status") == "queued"


def test_novel_outside_trial_is_readable_but_locked(client, user):
    novel_id = save_legacy_novel(user.id)
    save_episodes(novel_id, 1)

    assert client.get(f"/api/novels/{novel_id}", headers=user.headers).status_code == 200
    assert client.get(f"/api/novels/{novel_id}/episodes/1", headers=user.headers).status_code == 200

    assert_upgrade_required(client.post(f"/api/novels/{novel_id}/write-next", headers=user.headers), "novel_locked")
    assert_upgrade_required(
        client.post(f"/api/novels/{novel_id}/episodes/1/generate-image", headers=user.headers), "novel_locked"
    )
    res = client.post(f"/api/novels/{novel_id}/auto-continue", json={"auto_continue": True}, headers=user.headers)
    assert_upgrade_required(res, "novel_locked")
    assert store.get_blueprint(novel_id).auto_continue is False

    # Turning auto-continue off is always allowed.
    res = client.post(f"/api/novels/{novel_id}/auto-continue", json={"auto_continue": False}, headers=user.headers)
    assert res.status_code == 200


def test_legacy_novel_does_not_consume_the_trial(client, user):
    save_legacy_novel(user.id)
    assert trial_status(client, user)["trial_available"] is True
    create_trial_novel(client, user)


def test_chapter_allowance_holds_even_if_the_blueprint_claims_more(client, user):
    novel_id = create_trial_novel(client, user)
    save_episodes(novel_id, 3)
    bp = store.get_blueprint(novel_id)
    bp.total_episodes = 10  # simulate tampered or stale stored data
    store.save_blueprint(bp)

    assert_upgrade_required(client.post(f"/api/novels/{novel_id}/write-next", headers=user.headers), "trial_limit")
    res = client.post(f"/api/novels/{novel_id}/auto-continue", json={"auto_continue": True}, headers=user.headers)
    assert_upgrade_required(res, "trial_limit")


def test_trial_is_exhausted_once_all_chapters_are_written(client, user):
    novel_id = create_trial_novel(client, user)
    save_episodes(novel_id, 3)
    status = trial_status(client, user)
    assert status["chapters_written"] == 3
    assert status["chapters_remaining"] == 0
    assert status["trial_exhausted"] is True
    # The novel is also full, so this is the "nothing left to write" guard rather than an upgrade case.
    res = client.post(f"/api/novels/{novel_id}/write-next", headers=user.headers)
    assert res.status_code == 400
    assert "already been generated" in res.json()["detail"]


def test_locked_novel_image_is_served_locally_without_generating(client, user):
    """GET on a missing illustration must not spend model capacity for a locked novel."""
    novel_id = save_legacy_novel(user.id)
    save_episodes(novel_id, 1)
    assert store.get_episode_image(novel_id, 1) is None

    res = client.get(f"/api/novels/{novel_id}/episodes/1/image", headers=user.headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in res.content
    # Nothing was generated or persisted, so real artwork is still possible after an upgrade.
    assert store.get_episode_image(novel_id, 1) is None


def test_new_novel_request_rejects_out_of_range_chapter_counts(client, user):
    for bad in (-1, 0, 999):
        res = client.post("/api/novels", json=novel_payload(total_episodes=bad), headers=user.headers)
        assert res.status_code in (403, 422), f"{bad} -> {res.status_code} {res.text}"
        assert res.status_code != 500
    assert trial_status(client, user)["trial_available"] is True


def test_trial_used_is_reported_before_size_limits(client, user):
    create_trial_novel(client, user)
    # Oversized *and* ineligible: the real reason is the spent trial.
    res = client.post("/api/novels", json=novel_payload(title="Big", total_episodes=10), headers=user.headers)
    assert_upgrade_required(res, "trial_used")


def test_pipeline_refuses_locked_novel_without_calling_the_model(user):
    novel_id = save_legacy_novel(user.id)
    assert asyncio.run(generate_single_episode(novel_id, 1)) is None
    assert store.list_episodes(novel_id) == []
    assert store.get_progress(novel_id).current_step == "Free trial limit reached"


def test_auto_continue_loop_stops_at_the_trial_allowance(client, user):
    novel_id = create_trial_novel(client, user)
    save_episodes(novel_id, 3)
    bp = store.get_blueprint(novel_id)
    bp.total_episodes = 10
    bp.auto_continue = True
    store.save_blueprint(bp)

    asyncio.run(run_novel_episodes(novel_id))

    bp = store.get_blueprint(novel_id)
    assert bp.auto_continue is False
    assert bp.status != NovelStatus.FAILED.value
    assert len(store.list_episodes(novel_id)) == 3
    progress = store.get_progress(novel_id)
    assert progress.current_step == "Free trial limit reached"
    assert progress.is_active is False


def test_another_user_cannot_use_someone_elses_trial_novel(client, make_user):
    owner, other = make_user(), make_user()
    novel_id = create_trial_novel(client, owner)
    res = client.post(f"/api/novels/{novel_id}/write-next", headers=other.headers)
    assert res.status_code == 403
    # Refused on ownership (a plain string detail), not routed to the waitlist.
    assert isinstance(res.json()["detail"], str)
    assert trial_status(client, other)["trial_available"] is True
