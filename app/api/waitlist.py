from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.api.deps import get_current_user
from app.storage.users import User
from app.storage.waitlist import waitlist_manager

router = APIRouter(prefix="/api/waitlist", tags=["Waitlist"])

class WaitlistJoinRequest(BaseModel):
    email: Optional[str] = None

class WaitlistStatusResponse(BaseModel):
    joined: bool
    outcome: Optional[str] = None  # "joined" | "already_joined" | "updated" on POST
    email: Optional[str] = None
    joined_at: Optional[str] = None
    message: str = ""

@router.get("/me", response_model=WaitlistStatusResponse)
async def get_my_waitlist_entry(user: User = Depends(get_current_user)):
    """Whether the signed-in account is on the upgrade waitlist. Only ever returns the caller's own entry."""
    entry = waitlist_manager.get_for_user(user.id)
    if not entry:
        return WaitlistStatusResponse(joined=False)
    return WaitlistStatusResponse(joined=True, email=entry.email, joined_at=entry.created_at)

@router.post("", response_model=WaitlistStatusResponse)
async def join_waitlist(req: WaitlistJoinRequest, user: User = Depends(get_current_user)):
    """Adds the signed-in account to the upgrade waitlist (idempotent). Defaults to the account email."""
    try:
        entry, outcome = waitlist_manager.join(user.id, req.email if req.email is not None else user.email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    messages = {
        "joined": "You're on the waitlist. We'll email you when upgraded plans open.",
        "already_joined": "You're already on the waitlist. We'll email you when upgraded plans open.",
        "updated": "Your waitlist email has been updated."
    }
    return WaitlistStatusResponse(
        joined=True, outcome=outcome, email=entry.email, joined_at=entry.created_at, message=messages[outcome]
    )

@router.delete("/me")
async def leave_waitlist(user: User = Depends(get_current_user)):
    """Removes the signed-in account's waitlist entry and its stored email."""
    waitlist_manager.leave(user.id)
    return {"joined": False, "message": "You have been removed from the waitlist."}
