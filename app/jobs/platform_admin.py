"""Manage platform admin accounts from the command line.

The first platform admin can't be created through the API (nobody can make
themselves one), so it starts here. Later admins can be granted by an
existing admin with POST /api/v1/platform/admins, or here.

    make platform-admin ARGS="grant ops@example.com"            # existing user
    make platform-admin ARGS="grant ops@example.com --create --name 'Ops'"
    make platform-admin ARGS="revoke ops@example.com"
    make platform-admin ARGS="list"

--create makes a staff account with no organization; the password is asked
for interactively (never pass it on the command line: it lands in shell
history).
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.core.db import sessionmanager
from app.core.exceptions import APIError
from app.core.security import hash_password, utcnow
from app.models import User
from app.services import platform as platform_service

MIN_PASSWORD = 10


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _ask_password() -> str:
    first = getpass.getpass("Password for the new account: ")
    if len(first) < MIN_PASSWORD or first != first.strip():
        sys.exit(
            f"Password must be at least {MIN_PASSWORD} characters, no outer spaces"
        )
    if getpass.getpass("Repeat password: ") != first:
        sys.exit("Passwords don't match")
    return first


async def run(args: argparse.Namespace) -> None:
    sessionmanager.init_db()
    assert sessionmanager.session_factory is not None
    try:
        async with sessionmanager.session_factory() as session:
            if args.command == "list":
                for a in await platform_service.list_admins(session):
                    _out(f"{a.email}\t{a.name}\tsince {a.created_at:%Y-%m-%d}")
                return

            user = await session.scalar(
                select(User).where(User.email == args.email, User.deleted_at.is_(None))
            )
            if args.command == "grant":
                if user is None:
                    if not args.create:
                        sys.exit("No user with this email (add --create to make one)")
                    session.add(
                        User(
                            id=uuid.uuid4(),
                            email=args.email,
                            name=args.name or args.email.split("@")[0],
                            password_hash=hash_password(_ask_password()),
                            email_verified_at=utcnow(),
                        )
                    )
                    await session.flush()
                await platform_service.grant_admin(session, args.email, granted_by=None)
                _out(f"{args.email} is now a platform admin")
            elif args.command == "revoke":
                if user is None:
                    sys.exit("No user with this email")
                await platform_service.revoke_admin(session, user.id, revoked_by=None)
                _out(f"{args.email} is no longer a platform admin")
    except APIError as exc:
        sys.exit(exc.message)
    finally:
        await sessionmanager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant", help="Make a user a platform admin")
    grant.add_argument("email")
    grant.add_argument(
        "--create", action="store_true", help="Create the user if missing"
    )
    grant.add_argument("--name", help="Name for a created user")
    revoke = sub.add_parser("revoke", help="Remove platform admin role")
    revoke.add_argument("email")
    sub.add_parser("list", help="List platform admins")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
