"""DB-backed settings storage: encrypted persistence, masking, audit.

Backs the admin settings API (Phase 1 of the settings-centralization
plan).  One row per explicitly-set Settings field; absent fields fall
back to the env/default chain during ConfigService snapshot composition.
Secret values are stored as ``{"__enc__": "<fernet token>"}`` under
``COURTIER_SETTINGS_KEY`` — when the key is missing, secret writes fail
closed (:class:`SettingsKeyMissing`) instead of storing plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from courtier.db.tables.setting import SettingsChangeTable, SettingsTable

logger = logging.getLogger(__name__)

_ENC_KEY = "__enc__"


class SettingsKeyMissing(RuntimeError):
    """COURTIER_SETTINGS_KEY is not configured but a secret write was
    attempted.  Fails closed — secrets never persist in plaintext."""


class FernetCodec:
    """Fernet codec derived from COURTIER_SETTINGS_KEY material.

    Accepts a raw Fernet key, 32-byte hex material, or any passphrase
    (stretched via sha256 — documented as weaker than real key material).
    """

    def __init__(self, raw: str) -> None:
        raw = raw.strip()
        try:
            Fernet(raw)
            key = raw.encode()
        except Exception:
            try:
                material = bytes.fromhex(raw)
            except ValueError:
                material = raw.encode()
            if len(material) < 32:
                material = hashlib.sha256(material).digest()
            key = base64.urlsafe_b64encode(material)
        self._fernet = Fernet(key)

    @classmethod
    def from_env(cls) -> "FernetCodec | None":
        """Build from COURTIER_SETTINGS_KEY, or None when unset."""
        raw = os.getenv("COURTIER_SETTINGS_KEY", "").strip()
        return cls(raw) if raw else None

    def encrypt(self, value: str) -> dict[str, str]:
        return {_ENC_KEY: self._fernet.encrypt(value.encode()).decode()}

    def decrypt(self, stored: Any) -> str | None:
        """Decrypt an encrypted payload; None on wrong key / bad payload."""
        token = stored.get(_ENC_KEY) if isinstance(stored, dict) else None
        if not isinstance(token, str):
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def _value_hash(value: Any) -> str:
    return hashlib.sha256(repr(value).encode()).hexdigest()


class SettingsStore:
    """Async persistence for DB-backed settings (settings/settings_changes)."""

    def __init__(self, db: Any, codec: FernetCodec | None = None) -> None:
        self._db = db
        self._codec = codec

    @property
    def codec(self) -> FernetCodec | None:
        return self._codec

    async def load_rows(self) -> list[SettingsTable]:
        from sqlalchemy import select

        async with self._db.session() as session:
            rows = (await session.execute(select(SettingsTable))).scalars().all()
            # Detach copies: callers must not depend on the session lifecycle.
            return [row for row in rows]

    async def load_overrides(self) -> tuple[dict[str, Any], list[str]]:
        """Return (field -> value overrides, unreadable secret keys).

        Secrets that cannot be decrypted (wrong COURIER_SETTINGS_KEY) are
        reported as unreadable and omitted — the snapshot then falls back
        to the env/default value for that field; plaintext never leaks."""
        overrides: dict[str, Any] = {}
        unreadable: list[str] = []
        for row in await self.load_rows():
            if row.is_secret:
                if self._codec is None:
                    unreadable.append(row.key)
                    continue
                plain = self._codec.decrypt(row.value)
                if plain is None:
                    unreadable.append(row.key)
                    logger.warning(
                        "settings key %r is marked secret but cannot be decrypted "
                        "(wrong COURTIER_SETTINGS_KEY?); keeping env/default value",
                        row.key,
                    )
                    continue
                overrides[row.key] = plain
            else:
                overrides[row.key] = row.value
        return overrides, unreadable

    async def save(
        self,
        changes: dict[str, Any],
        *,
        actor: str,
        meta: dict[str, Any] | None = None,
    ) -> list[str]:
        """Upsert *changes* (field -> value) and append audit rows.

        ``meta`` maps field -> SettingMeta (defaults to config.SETTINGS_META);
        fields absent from the meta registry are rejected.  Returns the
        list of saved field names.  Raises SettingsKeyMissing when a secret
        is included without a configured codec."""
        from courtier.config import SETTINGS_META

        meta = meta if meta is not None else SETTINGS_META
        unknown = [k for k in changes if k not in meta]
        if unknown:
            raise KeyError(f"unknown or non-editable settings fields: {sorted(unknown)}")

        pending_secrets = [k for k in changes if meta[k].is_secret]
        if pending_secrets and self._codec is None:
            raise SettingsKeyMissing(
                "COURTIER_SETTINGS_KEY is not configured; secret settings "
                f"cannot be saved: {sorted(pending_secrets)}"
            )

        from sqlalchemy import select

        async with self._db.session() as session:
            existing: dict[str, SettingsTable] = {
                row.key: row
                for row in (
                    await session.execute(
                        select(SettingsTable).where(SettingsTable.key.in_(list(changes)))
                    )
                ).scalars()
            }
            saved: list[str] = []
            for key, value in changes.items():
                is_secret = meta[key].is_secret
                old_row = existing.get(key)
                old_value = None
                if old_row is not None:
                    old_value = old_row.value  # hashed as stored (mask for secrets)

                if is_secret and self._codec is not None:
                    stored: Any = self._codec.encrypt(str(value))
                else:
                    stored = value

                if old_row is None:
                    session.add(
                        SettingsTable(
                            key=key,
                            value=stored,
                            is_secret=is_secret,
                            category=meta[key].category,
                            updated_by=actor,
                        )
                    )
                else:
                    old_row.value = stored
                    old_row.is_secret = is_secret
                    old_row.category = meta[key].category
                    old_row.updated_by = actor
                session.add(
                    SettingsChangeTable(
                        key_name=key,
                        old_hash=_value_hash(old_value),
                        new_hash=_value_hash(stored),
                        actor=actor,
                    )
                )
                saved.append(key)
            return saved

    async def current_version(self) -> int:
        """Global settings version = MAX(settings_changes.id); 0 when empty.

        The ConfigService snapshot version reads this so staleness is
        detectable across restarts."""
        from sqlalchemy import func, select

        async with self._db.session() as session:
            version = (
                await session.execute(select(func.max(SettingsChangeTable.id)))
            ).scalar()
            return int(version or 0)

    async def load_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(SettingsChangeTable).order_by(
                            SettingsChangeTable.id.desc()
                        ).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": row.id,
                    "key": row.key_name,
                    "old_hash": row.old_hash,
                    "new_hash": row.new_hash,
                    "actor": row.actor,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
