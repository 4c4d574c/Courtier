"""DB-backed settings storage: encrypted persistence, masking, audit.

Backs the admin settings API (Phase 1 of the settings-centralization
plan).  One row per explicitly-set Settings field; absent fields fall
back to the env/default chain during ConfigService snapshot composition.
Secret values are stored as ``{"__enc__": "<fernet token>"}`` under
``COURTIER_SETTINGS_KEY`` — when the key is missing, secret writes fail
closed (:class:`SettingsKeyMissing`) instead of storing plaintext.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets as _py_secrets
from contextlib import contextmanager
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from courtier.db.tables.setting import SettingsChangeTable, SettingsTable

logger = logging.getLogger(__name__)

_ENC_KEY = "__enc__"


def _json_safe(value: Any) -> Any:
    """Normalize structured settings values for storage and comparison.

    Structured fields (``llm_model_pool``, ``guardrail_guards``) are pydantic
    models or lists/dicts of them — they must never reach the JSON column or
    ``!=`` comparisons as instances (the column cannot serialize them, and
    class identity is unstable across the config-module reload in the test
    suite)."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


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
        """Build from COURTIER_SETTINGS_KEY, or None when unset.

        Reads the process env first, then the .env file (see
        courtier.config.get_settings_encryption_key) so dev runs work
        without exported variables."""
        from courtier.config import get_settings_encryption_key

        raw = get_settings_encryption_key()
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

    async def key_has_history(self, key: str) -> bool:
        """True when the audit trail has any row for *key*.

        One-shot migrations gate on this instead of row presence so an
        admin clearing the migrated field (delete writes audit rows too)
        never gets silently re-seeded on the next refresh."""
        from sqlalchemy import select

        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(SettingsChangeTable.id)
                    .where(SettingsChangeTable.key_name == key)
                    .limit(1)
                )
            ).first()
            return row is not None

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

    async def delete(self, keys: list[str], *, actor: str) -> list[str]:
        """Remove rows so the fields fall back to the env/default chain.

        Writes audit rows with new_hash=None.  Returns the removed keys."""
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        async with self._db.session() as session:
            existing = {
                row.key: row
                for row in (
                    await session.execute(select(SettingsTable).where(SettingsTable.key.in_(keys)))
                ).scalars()
            }
            removed: list[str] = []
            for key in keys:
                row = existing.get(key)
                if row is None:
                    continue
                await session.execute(sa_delete(SettingsTable).where(SettingsTable.key == key))
                session.add(
                    SettingsChangeTable(
                        key_name=key,
                        old_hash=_value_hash(row.value),
                        new_hash=None,
                        actor=actor,
                    )
                )
                removed.append(key)
            return removed

    async def current_version(self) -> int:
        """Global settings version = MAX(settings_changes.id); 0 when empty.

        The ConfigService snapshot version reads this so staleness is
        detectable across restarts."""
        from sqlalchemy import func, select

        async with self._db.session() as session:
            version = (await session.execute(select(func.max(SettingsChangeTable.id)))).scalar()
            return int(version or 0)

    async def load_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import select

        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(SettingsChangeTable)
                        .order_by(SettingsChangeTable.id.desc())
                        .limit(limit)
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


# ---------------------------------------------------------------------------
# Snapshot composition (ConfigService DB integration)
# ---------------------------------------------------------------------------


@contextmanager
def _masked_settings_env():
    """Temporarily mask every env name Settings reads (alias or field
    name, upper-cased) so a defaults-only instance can be constructed."""
    from courtier.config import Settings

    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names.add((field.alias or name).upper())
        names.add(name.upper())
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k in names}
    try:
        yield
    finally:
        os.environ.update(saved)


def _defaults_settings() -> Any:
    """A Settings instance with no env/.env values — pure model defaults."""
    from courtier.config import Settings

    with _masked_settings_env():
        return Settings(_env_file=None)


def _cors_env_escape_active() -> bool:
    """True when CORS_ORIGINS is explicitly set in the environment — the
    admin lockout rescue: the env value always overrides the DB value."""
    return bool(os.getenv("CORS_ORIGINS", "").strip())


def compose_snapshot(base: Any, overrides: dict[str, Any]) -> Any:
    """Merge DB *overrides* onto the env-effective *base* snapshot.

    Precedence: model defaults < env/.env (base) < DB.  Tier-0 fields are
    stripped defensively (the store already rejects writing them), and an
    explicitly set CORS_ORIGINS env wins over any DB value.  Re-validates
    the merged whole so bad values fail loudly instead of half-applying."""
    from courtier.config import TIER0_SETTING_FIELDS, Settings

    filtered = {k: v for k, v in overrides.items() if k not in TIER0_SETTING_FIELDS}
    if _cors_env_escape_active():
        filtered.pop("cors_origins", None)
        filtered.pop("cors_allow_credentials", None)
    merged = {**base.model_dump(), **filtered}
    return Settings.model_validate(merged)


async def seed_from_env(
    store: "SettingsStore", base: Any, *, actor: str = "system-seed"
) -> list[str]:
    """One-shot import of non-default env/.env values into the DB store.

    Only fields whose env-effective value differs from the pure model
    default are imported (no 60-row default noise).  Secret fields are
    skipped with a warning when no encryption key is configured — the
    import never fails closed on the whole batch because of them."""
    from courtier.config import SETTINGS_META

    defaults = _defaults_settings()
    changed = {
        k: _json_safe(getattr(base, k))
        for k in SETTINGS_META
        if _json_safe(getattr(base, k)) != _json_safe(getattr(defaults, k))
    }
    if not changed:
        return []

    # Secrets seed alongside the rest when an encryption key exists;
    # without one they are skipped (fail-closed) instead of failing the
    # whole import batch.
    seedable = {
        k: v
        for k, v in changed.items()
        if not SETTINGS_META[k].is_secret or store.codec is not None
    }
    skipped_secrets = sorted(set(changed) - set(seedable))
    if skipped_secrets:
        logger.warning(
            "seed import skipped secret fields (COURTIER_SETTINGS_KEY unset): %s",
            skipped_secrets,
        )
    if not seedable:
        return []
    return await store.save(seedable, actor=actor)


async def refresh_settings_snapshot(
    service: Any,
    store: "SettingsStore | None",
    *,
    base: Any = None,
) -> dict[str, Any]:
    """Compose env base + DB overrides and swap the snapshot in-place.

    Runs at startup and after admin settings saves.  Degrades to the
    env-only snapshot (mode="env") when *store* is None or the DB is
    unreachable.  Includes one-shot .env seeding (first start, empty
    audit trail), the one-shot scalar→model-pool migration, the one-shot
    baseline guard-declaration seed, and JWT-secret bootstrap (generate +
    encrypt + save when empty and an encryption key exists)."""
    from courtier.config import get_settings

    info: dict[str, Any] = {
        "mode": "env",
        "version": service.version,
        "unreadable": [],
        "seeded": [],
        "jwt_generated": False,
    }
    base = base if base is not None else get_settings()
    if store is None:
        return info

    try:
        overrides, unreadable = await store.load_overrides()
    except Exception:
        logger.warning("settings store unavailable; staying env-only", exc_info=True)
        return info

    seeded: list[str] = []
    if await store.current_version() == 0:
        try:
            seeded = await seed_from_env(store, base)
            if seeded:
                overrides, unreadable = await store.load_overrides()
                logger.info(
                    "seeded %d setting(s) from env into DB: %s", len(seeded), sorted(seeded)
                )
        except Exception:
            logger.warning(
                "settings seed import failed; continuing with stored overrides only",
                exc_info=True,
            )

    pool_advanced_baked = False
    existing_pool = overrides.get("llm_model_pool")
    if isinstance(existing_pool, dict) and not existing_pool.get("advanced_defaults_materialized"):
        # One-shot legacy migration: pools saved before the per-model
        # advanced params existed carry None fields (inherit-invisible-
        # scalars semantics). Materialize the current scalar defaults into
        # them once; admin saves keep entries filled from here on.
        try:
            _bake_advanced_defaults(existing_pool, base)
            await store.save({"llm_model_pool": existing_pool}, actor="system-adv-defaults")
            overrides, unreadable = await store.load_overrides()
            pool_advanced_baked = True
            logger.info("materialized scalar defaults into existing model pool entries")
        except Exception:
            logger.warning("model pool defaults materialization failed", exc_info=True)

    pool_seeded = False
    if (
        "llm_model_pool" not in overrides
        and store.codec is not None
        and base.llm_base_url
        and base.llm_model
        and not await store.key_has_history("llm_model_pool")
    ):
        try:
            await _seed_model_pool(store, base)
            overrides, unreadable = await store.load_overrides()
            pool_seeded = True
            logger.info("seeded single-endpoint model pool from scalar llm_* settings")
        except Exception:
            logger.warning("model pool migration seed failed; continuing", exc_info=True)

    guards_seeded = False
    if "guardrail_guards" not in overrides and not await store.key_has_history("guardrail_guards"):
        # One-shot baseline seeding: the five built-in guards land as real
        # rows so the admin UI edits actual values. key_has_history keeps an
        # admin-emptied list from being re-seeded on the next start.
        try:
            from courtier.agent.core.guardrails.registry import DEFAULT_GUARD_DECLARATIONS

            await store.save(
                {
                    "guardrail_guards": [
                        {
                            "name": d.name,
                            "class_path": d.class_path,
                            "scope": d.scope,
                            "enabled": d.enabled,
                            "builtin": d.builtin,
                        }
                        for d in DEFAULT_GUARD_DECLARATIONS
                    ]
                },
                actor="system-guard-seed",
            )
            overrides, unreadable = await store.load_overrides()
            guards_seeded = True
            logger.info("seeded baseline guard declarations (guardrail_guards)")
        except Exception:
            logger.warning("guardrail_guards baseline seed failed; continuing", exc_info=True)

    jwt_generated = False
    if not overrides.get("jwt_secret") and not base.jwt_secret:
        if store.codec is not None:
            new_secret = _py_secrets.token_urlsafe(48)
            await store.save({"jwt_secret": new_secret}, actor="system-jwt-bootstrap")
            overrides["jwt_secret"] = new_secret
            jwt_generated = True
        else:
            logger.warning(
                "jwt_secret is empty and COURTIER_SETTINGS_KEY is unset; "
                "JWT bootstrap skipped (configure the key or set JWT_SECRET)"
            )

    snapshot = compose_snapshot(base, overrides)
    service.replace(snapshot)
    service.source = "db"
    # API-facing version: the durable store sequence (MAX audit id), not
    # the process-local replace counter, so GET and PUT agree across
    # restarts and workers.
    info.update(
        mode="db",
        version=await store.current_version(),
        unreadable=unreadable,
        seeded=seeded,
        pool_seeded=pool_seeded,
        pool_advanced_baked=pool_advanced_baked,
        guards_seeded=guards_seeded,
        jwt_generated=jwt_generated,
    )
    return info


def _bake_advanced_defaults(pool_doc: dict, base: Any) -> None:
    """Fill every None advanced field of every pool model entry from the
    scalar llm_* defaults (shared by the startup one-shot and the admin
    save path's equivalent)."""
    defaults: dict = {
        "context_window_tokens": base.llm_context_window_tokens,
        "max_tokens": base.llm_max_tokens,
        "temperature": base.llm_temperature,
        "timeout_seconds": base.llm_timeout,
        "frequency_penalty": base.llm_frequency_penalty,
        "presence_penalty": base.llm_presence_penalty,
    }
    if base.llm_extra_body is not None:
        defaults["extra_body"] = base.llm_extra_body
    for endpoint in pool_doc.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        for model in endpoint.get("models", []):
            if not isinstance(model, dict):
                continue
            for key, value in defaults.items():
                if model.get(key) is None:
                    model[key] = value
    pool_doc["advanced_defaults_materialized"] = True


async def _seed_model_pool(store: "SettingsStore", base: Any) -> None:
    """One-shot legacy→pool migration (called from refresh_settings_snapshot).

    Existing deployments configure the chat LLM through the scalar
    ``llm_base_url / llm_api_key / llm_model`` settings; seed them as a
    single-endpoint pool so the pool selection path is active everywhere
    with no admin action.  Idempotence is enforced by the caller (audit
    history gate), so an admin clearing the pool afterwards is respected."""
    from courtier.config import ModelPoolConfig, PoolEndpointConfig, PoolModelConfig

    pool = ModelPoolConfig(
        endpoints=[
            PoolEndpointConfig(
                id="ep_main",
                name="默认接入点",
                base_url=base.llm_base_url,
                enabled=True,
                models=[PoolModelConfig(id="mdl_main", name=base.llm_model, model=base.llm_model)],
            )
        ],
        default_model_id="mdl_main",
    )
    changes: dict[str, Any] = {"llm_model_pool": pool.model_dump()}
    if base.llm_api_key:
        # Secrets persist via Fernet(str(value)) — the map must be a JSON
        # string, not a dict (str(dict) would store Python repr).
        changes["llm_endpoint_keys"] = json.dumps({"ep_main": base.llm_api_key})
    await store.save(changes, actor="system-migrate")


# ---------------------------------------------------------------------------
# Connection probing (validate rebuild-group changes BEFORE persisting)
# ---------------------------------------------------------------------------


async def probe_connections(settings: Any, targets: set[str]) -> dict[str, str]:
    """Probe the given targets ("es" | "minio" | "plugins") against the
    *settings* snapshot; returns target -> error message (empty dict = all
    healthy).  Used pre-save so a bad endpoint/credential never persists."""
    errors: dict[str, str] = {}

    if "es" in targets and settings.es_hosts:
        try:
            from elasticsearch import Elasticsearch

            hosts = [h.strip() for h in settings.es_hosts.split(",") if h.strip()]
            kwargs: dict[str, Any] = {"request_timeout": 5}
            if settings.es_username and settings.es_password:
                kwargs["basic_auth"] = (settings.es_username, settings.es_password)
            es_client = Elasticsearch(hosts, **kwargs)
            try:
                healthy = await asyncio.to_thread(es_client.ping)
            finally:
                es_client.close()
            if not healthy:
                errors["es"] = "ES ping 失败（地址或认证错误？）"
        except Exception as exc:
            errors["es"] = str(exc)[:200]

    if "minio" in targets and settings.minio_endpoint:
        try:
            from minio import Minio

            minio_client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            # A bad endpoint must not hang the settings PUT for minutes.
            await asyncio.wait_for(
                asyncio.to_thread(minio_client.list_buckets), timeout=5.0
            )
        except Exception as exc:
            errors["minio"] = str(exc)[:200]

    if "plugins" in targets:
        try:
            endpoints = settings.plugin_endpoints()
        except Exception as exc:
            errors["plugins"] = f"COURTIER_PLUGIN_ENDPOINTS 格式错误: {exc}"
            endpoints = {}
        for name, (host, port) in endpoints.items():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0
                )
                writer.close()
                await writer.wait_closed()
            except Exception:
                errors.setdefault("plugins", f"插件 {name} ({host}:{port}) 不可达")
                break

    return errors
