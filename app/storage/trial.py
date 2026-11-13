"""Free trial entitlement.

Each non-admin account gets exactly one trial: a single novel with a capped number of
chapters and a capped chapter length. The trial is consumed when that novel is created and
is recorded on the user row, so deleting the novel does not hand the trial back.

Generation rights follow the novel's owner:
  - novels owned by an admin (or with no resolvable owner, which only admins can reach) are unlimited;
  - a regular author's trial novel may generate up to the trial allowance;
  - any other novel of a regular author is read-only until they upgrade.
"""
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel
from app.config import settings
from app.storage.database import db

TRIAL_USED = "trial_used"
TRIAL_LIMIT = "trial_limit"
NOVEL_LOCKED = "novel_locked"

WAITLIST_HINT = "Join the upgrade waitlist and we'll email you when larger plans open."


class TrialDenial(BaseModel):
    code: str
    message: str


class TrialStatus(BaseModel):
    plan: str  # "trial" or "unlimited"
    trial_available: bool = False
    trial_used: bool = False
    trial_exhausted: bool = False
    trial_started_at: Optional[str] = None
    trial_novel_id: Optional[str] = None
    trial_novel_exists: bool = False
    chapters_written: int = 0
    chapters_limit: int = 0
    chapters_remaining: int = 0
    max_words_per_chapter: int = 0
    waitlist_joined: bool = False


class TrialManager:
    @property
    def max_episodes(self) -> int:
        return max(1, int(settings.trial_max_episodes))

    @property
    def max_words_per_episode(self) -> int:
        return max(500, int(settings.trial_max_words_per_episode))

    def is_exempt_role(self, role: Optional[str]) -> bool:
        return role == "admin"

    def trial_used_denial(self) -> TrialDenial:
        return TrialDenial(
            code=TRIAL_USED,
            message=f"Your free trial has already been used. Each account includes one trial novel. {WAITLIST_HINT}"
        )

    def check_new_novel_limits(self, total_episodes: Optional[int], words_per_episode: Optional[int]) -> Optional[TrialDenial]:
        """Validates explicitly requested sizes for a trial novel. None means 'use the trial maximum'."""
        if total_episodes is not None and total_episodes > self.max_episodes:
            return TrialDenial(
                code=TRIAL_LIMIT,
                message=f"The free trial covers up to {self.max_episodes} chapters per novel. {WAITLIST_HINT}"
            )
        if words_per_episode is not None and words_per_episode > self.max_words_per_episode:
            return TrialDenial(
                code=TRIAL_LIMIT,
                message=f"The free trial covers up to {self.max_words_per_episode} words per chapter. {WAITLIST_HINT}"
            )
        return None

    def claim_trial(self, user_id: str, novel_id: str) -> bool:
        """Atomically consumes the account's single trial. Returns False if it was already used."""
        now = datetime.now(timezone.utc).isoformat()
        with db.get_connection() as conn:
            cur = conn.execute(
                "UPDATE users SET trial_novel_id = ?, trial_started_at = ? WHERE id = ? AND trial_started_at IS NULL",
                (novel_id, now, user_id)
            )
            return cur.rowcount == 1

    def release_trial(self, user_id: str, novel_id: str) -> None:
        """Rolls back a claim whose novel could not be saved. Never used for deleted novels."""
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET trial_novel_id = NULL, trial_started_at = NULL WHERE id = ? AND trial_novel_id = ?",
                (user_id, novel_id)
            )

    def check_episode_allowed(self, novel_id: str, owner_id: Optional[str], episode_number: int) -> Optional[TrialDenial]:
        """Returns a denial if generating this chapter of this novel is outside the owner's entitlement."""
        if not owner_id:
            return None
        with db.get_connection() as conn:
            row = conn.execute("SELECT role, trial_novel_id FROM users WHERE id = ?", (owner_id,)).fetchone()
        if not row or self.is_exempt_role(row["role"]):
            return None
        if row["trial_novel_id"] != novel_id:
            return TrialDenial(
                code=NOVEL_LOCKED,
                message=f"This novel is not part of your free trial, so new content can't be generated for it. You can still read it. {WAITLIST_HINT}"
            )
        if episode_number > self.max_episodes:
            return TrialDenial(
                code=TRIAL_LIMIT,
                message=f"Your free trial covers up to {self.max_episodes} chapters, and they have all been written. {WAITLIST_HINT}"
            )
        return None

    def get_status(self, user_id: str, role: Optional[str]) -> TrialStatus:
        with db.get_connection() as conn:
            joined = conn.execute("SELECT 1 FROM waitlist_entries WHERE user_id = ?", (user_id,)).fetchone() is not None
            if self.is_exempt_role(role):
                return TrialStatus(plan="unlimited", waitlist_joined=joined)

            row = conn.execute("SELECT trial_novel_id, trial_started_at FROM users WHERE id = ?", (user_id,)).fetchone()
            started_at = row["trial_started_at"] if row else None
            novel_id = row["trial_novel_id"] if row else None
            limit = self.max_episodes

            if not started_at:
                return TrialStatus(
                    plan="trial", trial_available=True,
                    chapters_limit=limit, chapters_remaining=limit,
                    max_words_per_chapter=self.max_words_per_episode, waitlist_joined=joined
                )

            novel = conn.execute("SELECT total_episodes FROM novels WHERE id = ?", (novel_id,)).fetchone() if novel_id else None
            written = 0
            if novel:
                limit = min(limit, novel["total_episodes"] or limit)
                written = conn.execute("SELECT COUNT(*) FROM episodes WHERE novel_id = ?", (novel_id,)).fetchone()[0]
            remaining = max(0, limit - written) if novel else 0

            return TrialStatus(
                plan="trial", trial_used=True, trial_exhausted=(remaining == 0),
                trial_started_at=started_at, trial_novel_id=novel_id, trial_novel_exists=bool(novel),
                chapters_written=min(written, limit), chapters_limit=limit, chapters_remaining=remaining,
                max_words_per_chapter=self.max_words_per_episode, waitlist_joined=joined
            )


trial_manager = TrialManager()
