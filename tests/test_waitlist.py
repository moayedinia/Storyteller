import threading

import pytest

from app.storage.database import db
from app.storage.waitlist import normalize_email, waitlist_manager


def entry_count(user_id: str) -> int:
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM waitlist_entries WHERE user_id = ?", (user_id,)).fetchone()[0]


def join(client, account, email=None):
    body = {} if email is None else {"email": email}
    return client.post("/api/waitlist", json=body, headers=account.headers)


# ---------- validation and normalisation ----------

@pytest.mark.parametrize("raw,expected", [
    ("  Foo.Bar@Example.COM ", "foo.bar@example.com"),
    ("first+tag@sub.example.co.uk", "first+tag@sub.example.co.uk"),
])
def test_normalize_email_accepts_and_canonicalises(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "plainaddress", "@example.com", "user@", "user@localhost", "user@@example.com",
    "user name@example.com", "user@exa mple.com", ".user@example.com", "us..er@example.com",
    "user@-example.com", "user@example..com", "user@example.com\nBcc: other@example.com",
    "a" * 65 + "@example.com", "user@" + "a" * 250 + ".com", None, 42,
])
def test_normalize_email_rejects_invalid_input(raw):
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_invalid_email_is_rejected_and_nothing_is_stored(client, user):
    for bad in ["not-an-email", "", "a@b", "user@example.com\r\nx"]:
        res = join(client, user, bad)
        assert res.status_code == 400, res.text
        assert isinstance(res.json()["detail"], str)
    assert entry_count(user.id) == 0
    assert client.get("/api/waitlist/me", headers=user.headers).json()["joined"] is False


# ---------- joining ----------

def test_join_defaults_to_account_email_before_trial_is_used(client, user):
    res = join(client, user)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["joined"] is True
    assert body["outcome"] == "joined"
    assert body["email"] == user.email

    me = client.get("/api/waitlist/me", headers=user.headers).json()
    assert me["joined"] is True and me["email"] == user.email
    assert client.get("/api/trial/status", headers=user.headers).json()["waitlist_joined"] is True


def test_join_stores_normalised_email(client, user):
    res = join(client, user, "  Mixed.Case@Example.COM  ")
    assert res.status_code == 200, res.text
    assert res.json()["email"] == "mixed.case@example.com"


def test_duplicate_submission_is_idempotent(client, user):
    first = join(client, user, "reader@example.com").json()
    second = join(client, user, "READER@example.com ")
    assert second.status_code == 200
    assert second.json()["outcome"] == "already_joined"
    assert second.json()["joined_at"] == first["joined_at"]
    assert entry_count(user.id) == 1


def test_resubmitting_a_different_email_updates_the_single_entry(client, user):
    first = join(client, user, "old@example.com").json()
    second = join(client, user, "new@example.com").json()
    assert second["outcome"] == "updated"
    assert second["email"] == "new@example.com"
    assert second["joined_at"] == first["joined_at"]  # place in line is kept
    assert entry_count(user.id) == 1


def test_concurrent_submissions_create_one_entry(user):
    barrier = threading.Barrier(8)
    errors = []

    def submit():
        barrier.wait()
        try:
            waitlist_manager.join(user.id, "race@example.com")
        except Exception as exc:  # pragma: no cover - surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert entry_count(user.id) == 1


def test_user_can_leave_the_waitlist(client, user):
    join(client, user)
    assert client.delete("/api/waitlist/me", headers=user.headers).status_code == 200
    assert entry_count(user.id) == 0


# ---------- authorization ----------

def test_waitlist_endpoints_require_authentication(client):
    assert client.post("/api/waitlist", json={"email": "anon@example.com"}).status_code == 401
    assert client.get("/api/waitlist/me").status_code == 401
    assert client.delete("/api/waitlist/me").status_code == 401
    assert client.get("/api/admin/waitlist").status_code == 401


def test_only_admins_can_list_or_delete_entries(client, make_user, admin):
    member, other = make_user(), make_user()
    join(client, member, "member@example.com")

    assert client.get("/api/admin/waitlist", headers=other.headers).status_code == 403
    assert client.delete("/api/admin/waitlist/anything", headers=other.headers).status_code == 403

    res = client.get("/api/admin/waitlist", headers=admin.headers)
    assert res.status_code == 200
    mine = [e for e in res.json() if e["user_id"] == member.id]
    assert len(mine) == 1
    assert mine[0]["email"] == "member@example.com"
    assert mine[0]["username"] == member.username

    assert client.delete(f"/api/admin/waitlist/{mine[0]['id']}", headers=admin.headers).status_code == 200
    assert entry_count(member.id) == 0
    assert client.delete(f"/api/admin/waitlist/{mine[0]['id']}", headers=admin.headers).status_code == 404


def test_users_only_ever_see_their_own_entry(client, make_user):
    member, other = make_user(), make_user()
    join(client, member, "private@example.com")
    body = client.get("/api/waitlist/me", headers=other.headers).json()
    assert body["joined"] is False
    assert body["email"] is None


def test_deleting_an_account_removes_its_waitlist_entry(client, make_user, admin):
    member = make_user()
    join(client, member)
    assert client.delete(f"/api/admin/users/{member.id}", headers=admin.headers).status_code == 200
    assert entry_count(member.id) == 0
