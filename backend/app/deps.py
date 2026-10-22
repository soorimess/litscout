"""FastAPI dependencies: settings, database session, current user, job lookup."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuthSession, Job, User
from .security import token_hash

COOKIE = "ls_session"


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE)
    if token:
        s = db.get(AuthSession, token_hash(token))
        if s and s.expires_at.replace(tzinfo=s.expires_at.tzinfo or timezone.utc) > datetime.now(timezone.utc):
            return s.user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def owned_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if not job:  # same answer for "missing" and "someone else's"
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return job
