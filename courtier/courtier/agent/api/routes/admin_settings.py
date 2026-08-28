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
    refresh_settings_snapshot,
)

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
        compose_snapshot(get_settings(), prospective)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(loc) for loc in e.get("loc", ())[1:]) or str(e.get("loc")),
             "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        raise HTTPException(422, detail={"message": "校验失败", "errors": errors}) from exc

    actor = str(user.get("sub") or "admin")
    if sets:
        await store.save(sets, actor=actor)
    if clears:
        await store.delete(clears, actor=actor)

    info = await refresh_settings_snapshot(get_config_service(), store)
    changed = list(sets) + clears
    return {
        "applied": sorted(sets),
        "cleared": clears,
        "version": info["version"],
        "restart_required": sorted(
            k for k in changed if SETTINGS_META[k].effect == "restart"
        ),
    }


@router.get("/audit")
async def get_audit(
    request: Request, limit: int = 50, user: dict = Depends(require_admin)
):
    store = _get_store(request)
    if store is None:
        raise HTTPException(503, "设置存储不可用（env-only 模式）")
    return {"changes": await store.load_audit(limit=min(max(limit, 1), 500))}


@router.post("/test/llm")
async def test_llm(
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    user: dict = Depends(require_admin),
):
    """One-token chat completion against the LLM endpoint.

    Body may carry llm_* field overrides so the admin can test BEFORE
    saving; absent fields use the effective snapshot."""
    from courtier.agent.core.backends.openai_backend import OpenAIModelBackend
    from courtier.agent.core.protocol import ChatMessage, ChatRequest

    overrides = {
        k: v for k, v in (body or {}).items() if k.startswith("llm_") and v is not None
    }
    try:
        target = compose_snapshot(get_settings(), overrides)
    except Exception as exc:
        raise HTTPException(422, f"待测试的 LLM 配置无效: {exc}") from exc

    if not target.llm_base_url or not target.llm_model:
        return {"ok": False, "error": "llm_base_url / llm_model 未配置"}

    backend = OpenAIModelBackend(
        base_url=target.llm_base_url,
        api_key=target.llm_api_key,
        model=target.llm_model,
        temperature=0.0,
        timeout=20.0,
    )
    try:
        response = await backend.chat(
            ChatRequest(
                model=target.llm_model,
                messages=(ChatMessage(role="user", content="回复一个字：ok"),),
                temperature=0.0,
                max_tokens=8,
            )
        )
        return {
            "ok": True,
            "model": target.llm_model,
            "reply": (response.message.content or "")[:50],
            "latency_ms": round(response.latency_ms),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        await backend.close()
