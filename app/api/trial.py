from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import get_current_user
from app.models.blueprint import StoryBlueprint
from app.storage.users import User
from app.storage.trial import TrialDenial, TrialStatus, trial_manager

router = APIRouter(prefix="/api/trial", tags=["Trial"])

def upgrade_required(denial: TrialDenial) -> HTTPException:
    """403 carrying a machine-readable code so the client can offer the waitlist instead of a bare error."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": denial.code, "message": denial.message, "upgrade_path": "waitlist"}
    )

def get_generation_denial(bp: StoryBlueprint, episode_number: int) -> Optional[TrialDenial]:
    return trial_manager.check_episode_allowed(bp.novel_id, bp.owner_id, episode_number)

def require_generation_allowed(bp: StoryBlueprint, episode_number: int) -> None:
    denial = get_generation_denial(bp, episode_number)
    if denial:
        raise upgrade_required(denial)

@router.get("/status", response_model=TrialStatus)
async def get_trial_status(user: User = Depends(get_current_user)):
    """The signed-in account's trial allowance, usage, and waitlist membership."""
    return trial_manager.get_status(user.id, user.role)
