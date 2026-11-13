"""Upgrade waitlist: one entry per account, holding the email to notify when paid plans open."""
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from pydantic import BaseModel
from app.storage.database import db

MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64
_EMAIL_RE = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class WaitlistEntry(BaseModel):
    id: str
    user_id: str
    email: str
    created_at: str
    updated_at: str


class WaitlistAdminEntry(WaitlistEntry):
    username: Optional[str] = None
    full_name: Optional[str] = None


def normalize_email(raw: Optional[str]) -> str:
    """Trims and lower-cases an address, raising ValueError with a user-safe message if it is not usable."""
    if not isinstance(raw, str):
        raise ValueError("Please enter an email address.")
    email = raw.strip().lower()
    if not email:
        raise ValueError("Please enter an email address.")
    if len(email) > MAX_EMAIL_LENGTH:
        raise ValueError("That email address is too long.")
    local, _, domain = email.rpartition("@")
    if (
        not local or not domain
        or len(local) > MAX_LOCAL_PART_LENGTH
        or local.startswith(".") or local.endswith(".") or ".." in local
        or not _EMAIL_RE.match(email)
    ):
        raise ValueError("Please enter a valid email address, like name@example.com.")
    return email


class WaitlistManager:
    def join(self, user_id: str, email: str) -> Tuple[WaitlistEntry, str]:
        """Adds or updates the account's entry. Returns (entry, outcome) with outcome in
        'joined' | 'already_joined' | 'updated'. Safe under duplicate and concurrent submissions:
        the UNIQUE(user_id) constraint guarantees a single row per account."""
        clean = normalize_email(email)
        now = datetime.now(timezone.utc).isoformat()
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT email FROM waitlist_entries WHERE user_id = ?", (user_id,)).fetchone()
            if existing is None:
                outcome = "joined"
            elif existing["email"] == clean:
                outcome = "already_joined"
            else:
                outcome = "updated"

            if outcome != "already_joined":
                conn.execute("""
                    INSERT INTO waitlist_entries (id, user_id, email, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET email = excluded.email, updated_at = excluded.updated_at
                """, (f"wl_{uuid.uuid4().hex[:12]}", user_id, clean, now, now))

            row = conn.execute("SELECT * FROM waitlist_entries WHERE user_id = ?", (user_id,)).fetchone()
            return WaitlistEntry(**dict(row)), outcome

    def get_for_user(self, user_id: str) -> Optional[WaitlistEntry]:
        with db.get_connection() as conn:
            row = conn.execute("SELECT * FROM waitlist_entries WHERE user_id = ?", (user_id,)).fetchone()
            return WaitlistEntry(**dict(row)) if row else None

    def leave(self, user_id: str) -> bool:
        with db.get_connection() as conn:
            return conn.execute("DELETE FROM waitlist_entries WHERE user_id = ?", (user_id,)).rowcount > 0

    def list_entries(self) -> List[WaitlistAdminEntry]:
        with db.get_connection() as conn:
            rows = conn.execute("""
                SELECT w.*, u.username, u.full_name
                FROM waitlist_entries w
                LEFT JOIN users u ON u.id = w.user_id
                ORDER BY w.created_at ASC
            """).fetchall()
            return [WaitlistAdminEntry(**dict(r)) for r in rows]

    def delete_entry(self, entry_id: str) -> bool:
        with db.get_connection() as conn:
            return conn.execute("DELETE FROM waitlist_entries WHERE id = ?", (entry_id,)).rowcount > 0


waitlist_manager = WaitlistManager()
