"""User credential & lifecycle service.

Owns the DB-side user operations that used to live in the auth/profile/
admin routes: credential checking with lockout, registration, password
changes (with refresh-token revocation), login bookkeeping, and the
expired-refresh-token sweep.  Routes stay thin: parse request → call
service → map errors to the transport.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update

from courtier.db.tables.refresh_token import RefreshTokenTable
from courtier.db.tables.user import UserStatus, UserTable

from ..middleware.auth import hash_password, verify_password

logger = logging.getLogger(__name__)

_MAX_FAILED_ATTEMPTS = 10
_LOCKOUT_DURATION = timedelta(minutes=15)


class UserNotFound(Exception):
    """The referenced user does not exist."""


class UsernameTaken(Exception):
    pass


class InvalidCredentials(Exception):
    """Wrong username or password (message is transport's to shape)."""


class AccountLocked(Exception):
    def __init__(self, remaining_seconds: int) -> None:
        super().__init__(f"locked for {remaining_seconds}s")
        self.remaining_seconds = remaining_seconds


class AccountNotApproved(Exception):
    pass


class AccountDisabled(Exception):
    pass


class WrongCurrentPassword(Exception):
    pass


async def find_by_username(db: Any, username: str) -> UserTable | None:
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == username)
        )
        return result.scalar_one_or_none()


async def authenticate(db: Any, *, username: str, password: str) -> UserTable:
    """Full credential check: lockout → bcrypt → status.  Raises the
    service exceptions above; the route maps them to HTTP responses."""
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == username)
        )
        user = result.scalar_one_or_none()

    if user is None:
        # Dummy bcrypt to normalize timing and prevent username enumeration
        _DUMMY_HASH = "$2b$12$LJ3m4ys3GZfnYMz8kVsKaOTSxGHLfEhCgJwN5B6Hm3VlOUlS3wFJq"
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentials

    # The column is a naive DateTime and drivers return naive UTC —
    # normalize before comparing against the aware clock.
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if locked_until is not None and locked_until > now:
        remaining = int((locked_until - now).total_seconds())
        raise AccountLocked(remaining)

    if not verify_password(password, user.password_hash):
        # Track failed attempt and potentially lock the account
        async with db.session() as session:
            merged = await session.merge(user)
            merged.failed_login_attempts += 1
            if merged.failed_login_attempts >= _MAX_FAILED_ATTEMPTS:
                # Naive UTC into the naive DateTime column (see read side).
                merged.locked_until = (now + _LOCKOUT_DURATION).replace(tzinfo=None)
                logger.warning(
                    "Account locked: %s (%d failed attempts)",
                    merged.username,
                    merged.failed_login_attempts,
                )
        raise InvalidCredentials

    # Verify status before resetting failure counters so that a
    # pending/disabled account does not have its lockout state cleared.
    if user.status == UserStatus.pending:
        raise AccountNotApproved
    if user.status == UserStatus.disabled:
        raise AccountDisabled
    return user


async def record_successful_login(
    db: Any, user: UserTable, token_hash: str, expires_at: datetime
) -> None:
    """Reset failure counters, mint the refresh row, and sweep expired ones
    — all inside one transaction so state stays consistent."""
    async with db.session() as session:
        merged = await session.merge(user)
        if merged.failed_login_attempts > 0 or merged.locked_until is not None:
            merged.failed_login_attempts = 0
            merged.locked_until = None
        session.add(
            RefreshTokenTable(
                user_id=user.id, token_hash=token_hash, expires_at=expires_at
            )
        )
        # Opportunistic hygiene: refresh rows are never read past expiry,
        # and without cleanup the table grows forever.  Sweep expired rows
        # older than a week while we are already in a transaction.
        await session.execute(
            delete(RefreshTokenTable).where(
                RefreshTokenTable.expires_at
                < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
            )
        )


async def create_pending_user(db: Any, *, username: str, password: str, email: str) -> int:
    """Create a status=pending user; raises UsernameTaken on conflict."""
    async with db.session() as session:
        result = await session.execute(
            select(UserTable).where(UserTable.username == username)
        )
        if result.scalar_one_or_none() is not None:
            raise UsernameTaken
        user = UserTable(
            username=username,
            password_hash=hash_password(password),
            email=email,
            status=UserStatus.pending,
        )
        session.add(user)
        await session.commit()
        return user.id


async def change_password(
    db: Any,
    *,
    user_id: int,
    current_password: str | None,
    new_password: str,
) -> None:
    """Self-service password change: verify the current password, update the
    hash, and revoke every live refresh token of the user (a stolen refresh
    cookie must not survive a credential change)."""
    from ..middleware.auth import revoke_all_refresh_tokens

    async with db.session() as session:
        result = await session.execute(select(UserTable).where(UserTable.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFound
        if current_password is None:
            raise WrongCurrentPassword
        if not verify_password(current_password, user.password_hash):
            raise WrongCurrentPassword
        await session.execute(
            update(UserTable)
            .where(UserTable.id == user_id)
            .values(password_hash=hash_password(new_password))
        )
        await revoke_all_refresh_tokens(user_id, session)
        await session.commit()
