"""Sign in and out. Accounts are created by an administrator (python -m app.cli create-user)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..deps import COOKIE, current_user, get_db
from ..models import AuthSession, User
from ..schemas import LoginIn, UserOut
from ..security import DUMMY_HASH, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> UserOut:
    s = request.app.state.settings
    ip = request.client.host if request.client else "?"
    if not request.app.state.login_limiter.allow(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many sign-in attempts. Wait a minute and try again.")
    user = db.scalar(select(User).where(User.username == body.username))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong user name or password")
    now = datetime.now(timezone.utc)
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    token = new_token()
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=now + timedelta(days=s.session_days)))
    db.commit()
    response.set_cookie(COOKIE, token, max_age=s.session_days * 86400, httponly=True, secure=s.cookie_secure,
                        samesite="strict", path="/")
    return UserOut(username=user.username)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut(username=user.username)
