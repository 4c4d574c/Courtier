"""Admin settings API — the DB-backed configuration surface.

GET returns the grouped schema (masked secrets, per-field source and
effect), PUT applies partial category updates (whole-model validation,
audit, snapshot swap), plus the audit trail and an LLM connectivity test.
All routes require the admin role.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from courtier.agent.api.routes.admin_users import require_admin
from courtier.config import (
    SETTING_CATEGORIES,
    SETTINGS_META,
    Settings,
    get_config_service,
    get_settings,
)
from courtier.settings_store import (
    _defaults_settings,
    compose_snapshot,
    probe_connections,
    refresh_settings_snapshot,
)

#: rebuild-group field prefixes → connection target to probe/invalidate.
_TARGET_BY_PREFIX = (
    ("es_", "es"),
    ("minio_", "minio"),
    ("courtier_plugin_", "plugins"),
)


def _rebuild_targets(changed: list[str]) -> set[str]:
    targets: set[str] = set()
    for name in changed:
        for prefix, target in _TARGET_BY_PREFIX:
            if name.startswith(prefix):
                targets.add(target)
    return targets


async def _reload_plugin_connections(request: Request) -> None:
    """Re-resolve endpoints/token and restart plugin connections so the
    new values take effect without an app restart."""
    system = getattr(request.app.state, "plugin_system", None)
    manager = getattr(system, "_manager", None)
    if manager is None:
        return
    try:
        manager._resolve_connection_config()
        for name in list(manager.get_processes().keys()):
            await manager.restart_plugin(name)
    except Exception:
        logger.warning("plugin connection reload failed", exc_info=True)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/settings", tags=["admin-settings"])


def _field_type(name: str) -> str:
    annotation = Settings.model_fields[name].annotation
    origin = getattr(annotation, "__origin__", None)
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "string"
    if origin is list or name == "cors_origins":
        return "list"
    return "json"


def _env_name(name: str) -> str:
    field = Settings.model_fields[name]
    return (field.alias or name).upper()


def _field_source(name: str, settings: Settings, overrides: dict, defaults: Settings) -> str:
    if name in overrides:
        return "db"
    return "env" if getattr(settings, name) != getattr(defaults, name) else "default"


def _secret_view(value: Any) -> dict[str, Any]:
    text = str(value or "")
    return {"set": bool(text), "tail": text[-4:] if text else ""}


def _get_store(request: Request):
    return getattr(request.app.state, "settings_store", None)


@router.get("")
async def get_settings_view(request: Request, user: dict = Depends(require_admin)):
    """Grouped settings schema with current values (secrets masked)."""
    store = _get_store(request)
    settings = get_settings()
    overrides: dict[str, Any] = {}
    unreadable: list[str] = []
    version: int | None = None
    mode = "env"
    if store is not None:
        try:
            overrides, unreadable = await store.load_overrides()
            version = await store.current_version()
            mode = "db"
        except Exception:
            logger.warning("settings store unreadable; serving env-only view", exc_info=True)
            mode = "env"
    defaults = _defaults_settings()

    categories = []
    for cat_key, label in SETTING_CATEGORIES.items():
        fields = []
        for name, meta in SETTINGS_META.items():
            if meta.category != cat_key:
                continue
            value = getattr(settings, name)
            if name in unreadable:
                rendered: Any = {"set": True, "unreadable": True}
            elif meta.is_secret:
                rendered = _secret_view(value)
            else:
                rendered = value
            fields.append(
                {
                    "name": name,
                    "category": cat_key,
                    "is_secret": meta.is_secret,
                    "effect": meta.effect,
                    "source": _field_source(name, settings, overrides, defaults),
                    "value": rendered,
                    "type": _field_type(name),
                    "env_name": _env_name(name),
                    "description": Settings.model_fields[name].description or "",
                }
            )
        if fields:
            categories.append({"key": cat_key, "label": label, "fields": fields})
    return {
        "mode": mode,
        "version": version,
        "unreadable": unreadable,
        "categories": categories,
    }


@router.put("/{category}")
async def update_category(
    category: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    user: dict = Depends(require_admin),
):
    """Partial update of one settings category.

    Value semantics: omitted = unchanged; null = clear (fall back to
    env/default); any other value = set.  The merged whole is re-validated
    before anything is persisted; the ConfigService snapshot is swapped
    atomically after the write."""
    from pydantic import ValidationError

    store = _get_store(request)
    if store is None:
        raise HTTPException(503, "设置存储不可用（env-only 模式，需要配置 MYSQL_URL）")

    category_fields = {
        name: meta for name, meta in SETTINGS_META.items() if meta.category == category
    }
    if not category_fields:
        raise HTTPException(404, f"未知的设置分组: {category!r}")

    unknown = sorted(k for k in body if k not in category_fields)
    if unknown:
        raise HTTPException(422, f"不属于分组 {category!r} 的字段: {unknown}")

    sets = {k: v for k, v in body.items() if v is not None}
    clears = sorted(k for k, v in body.items() if v is None)

    current_overrides, _ = await store.load_overrides()
    prospective = {**current_overrides, **sets}
    for key in clears:
        prospective.pop(key, None)
    try:
        prospective_snapshot = compose_snapshot(get_settings(), prospective)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(loc) for loc in e.get("loc", ())[1:]) or str(e.get("loc")),
             "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        raise HTTPException(422, detail={"message": "校验失败", "errors": errors}) from exc

    # Connection groups validate BEFORE persisting: a bad endpoint or
    # credential must never land in the DB (no save→rebuild→rollback).
    changed = list(sets) + clears
    targets = _rebuild_targets(changed)
    if targets:
        probe_errors = await probe_connections(prospective_snapshot, targets)
        if probe_errors:
            raise HTTPException(
                422,
                detail={"message": "连接测试失败，未保存", "errors": probe_errors},
            )

    actor = str(user.get("sub") or "admin")
    if sets:
        await store.save(sets, actor=actor)
    if clears:
        await store.delete(clears, actor=actor)

    info = await refresh_settings_snapshot(get_config_service(), store)

    # Hot rebuild: drop derived clients so the next use rebuilds from the
    # new snapshot; plugin connections re-resolve and reconnect.
    if "es" in targets:
        from courtier.es.client import invalidate_es_client

        invalidate_es_client()
    if "minio" in targets:
        from courtier.storage.client import invalidate_minio_client

        invalidate_minio_client()
    if "plugins" in targets:
        await _reload_plugin_connections(request)

    return {
        "applied": sorted(sets),
        "cleared": clears,
        "version": info["version"],
        "revalidated": sorted(targets),
        "restart_required": sorted(
            k for k in changed if SETTINGS_META[k].effect == "restart"
        ),
    }


@router.post("/jwt/rotate")
async def rotate_jwt_secret(
    request: Request, user: dict = Depends(require_admin)
):
    """Generate a fresh JWT secret (invalidates every session; the admin
    stays logged out too and must sign in again)."""
    import secrets as _secrets

    store = _get_store(request)
    if store is None:
        raise HTTPException(503, "设置存储不可用（env-only 模式）")
    if store.codec is None:
        raise HTTPException(
            422, "COURTIER_SETTINGS_KEY 未配置，无法安全存储新的 JWT 密钥"
        )

    new_secret = _secrets.token_urlsafe(48)
    await store.save({"jwt_secret": new_secret}, actor=str(user.get("sub") or "admin"))
    info = await refresh_settings_snapshot(get_config_service(), store)
    return {"ok": True, "version": info["version"], "sessions_invalidated": True}


@router.get("/audit")
async def get_audit(
    request: Request, limit: int = 50, user: dict = Depends(require_admin)
):
    store = _get_store(request)
    if store is None:
        raise HTTPException(503, "设置存储不可用（env-only 模式）")
    return {"changes": await store.load_audit(limit=min(max(limit, 1), 500))}


@router.post("/test/{target}")
async def test_target(
    target: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    user: dict = Depends(require_admin),
):
    """Connectivity test: llm (one-token completion), es / minio / plugins
    (probe against the prospective settings).

    Body may carry field overrides so the admin can test BEFORE saving;
    absent fields use the effective snapshot."""
    if target in ("es", "minio", "plugins"):
        overrides = {
            k: v for k, v in (body or {}).items() if v is not None and k in SETTINGS_META
        }
        try:
            snapshot = compose_snapshot(get_settings(), overrides)
        except Exception as exc:
            raise HTTPException(422, f"待测试的配置无效: {exc}") from exc
        errors = await probe_connections(snapshot, {target})
        return {"ok": not errors, "errors": errors}
    if target != "llm":
        raise HTTPException(404, f"未知的测试目标: {target!r}")

    from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
    from courtier.agent.core.protocol import ChatMessage, ChatRequest

    overrides = {
        k: v for k, v in (body or {}).items() if k.startswith("llm_") and v is not None
    }
    try:
        target_settings = compose_snapshot(get_settings(), overrides)
    except Exception as exc:
        raise HTTPException(422, f"待测试的 LLM 配置无效: {exc}") from exc

    if not target_settings.llm_base_url or not target_settings.llm_model:
        return {"ok": False, "error": "llm_base_url / llm_model 未配置"}

    backend = OpenAIModelBackend(
        base_url=target_settings.llm_base_url,
        api_key=target_settings.llm_api_key,
        model=target_settings.llm_model,
        temperature=0.0,
        timeout=20.0,
    )
    try:
        response = await backend.chat(
            ChatRequest(
                model=target_settings.llm_model,
                messages=(ChatMessage(role="user", content="回复一个字：ok"),),
                temperature=0.0,
                max_tokens=8,
            )
        )
        return {
            "ok": True,
            "model": target_settings.llm_model,
            "reply": (response.message.content or "")[:50],
            "latency_ms": round(response.latency_ms),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        await backend.close()
