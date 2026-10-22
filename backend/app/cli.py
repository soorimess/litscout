"""Account administration (there is no sign-up):

  python -m app.cli create-user --random          random user name and password, printed once
  python -m app.cli create-user --username NAME   prompts for a password
  python -m app.cli list-users | delete-user NAME | set-password NAME
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select

from .config import get_settings
from .db import Database
from .models import User
from .security import hash_password, random_password


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--username")
    c.add_argument("--random", action="store_true", help="random user name and password")
    sub.add_parser("list-users")
    sub.add_parser("delete-user").add_argument("username")
    sub.add_parser("set-password").add_argument("username")
    a = ap.parse_args()
    db = Database(get_settings().db_url)
    db.create_all()
    with db.sessions() as s:
        if a.cmd == "create-user":
            name = a.username or (f"reviewer-{secrets.token_hex(3)}" if a.random else None)
            if not name:
                sys.exit("Give --username NAME or --random")
            if s.scalar(select(User).where(User.username == name)):
                sys.exit(f"User {name} already exists")
            pw = random_password() if a.random else getpass.getpass("Password: ")
            if len(pw) < 12:
                sys.exit("Use at least 12 characters")
            s.add(User(username=name, password_hash=hash_password(pw)))
            s.commit()
            print(f"user name: {name}\npassword:  {pw}" if a.random else f"created {name}")
        elif a.cmd == "list-users":
            for u in s.scalars(select(User).order_by(User.id)):
                print(u.id, u.username, u.created_at.isoformat(timespec="seconds"))
        elif a.cmd == "delete-user":
            u = s.scalar(select(User).where(User.username == a.username))
            if not u:
                sys.exit("No such user")
            s.delete(u)
            s.commit()
            print(f"deleted {a.username} and their reviews from the web database (run folders stay)")
        elif a.cmd == "set-password":
            u = s.scalar(select(User).where(User.username == a.username))
            if not u:
                sys.exit("No such user")
            pw = getpass.getpass("New password: ")
            if len(pw) < 12:
                sys.exit("Use at least 12 characters")
            u.password_hash = hash_password(pw)
            s.commit()
            print("password changed")


if __name__ == "__main__":
    main()
