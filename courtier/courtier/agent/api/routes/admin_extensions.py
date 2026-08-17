"""Admin extension management routes — plugins and skills."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..rate_limiter import limiter
from ..services.skill_admin_service import (
    create_skill,
    set_skill_enabled,
)
from .admin_users import require_admin

router = APIRouter(prefix="/api/admin/extensions", tags=["admin"])


def _rediscover(request: Request) -> None:
    """Re-run domain discovery in-process so mutations take effect now."""
    from courtier.config import CourtierConfig

    current = request.app.state.courtier_config
    fresh = CourtierConfig.from_env(repo_root=current.repo_root)
    fresh.discover()
    request.app.state.courtier_config = fresh


def _resolve_skills_dir(request: Request, domain_name: str) -> str:
    """Resolve one enabled domain's skills directory by package name."""
    from ..services.domain_admin_service import list_domains_with_state

    for pkg in list_domains_with_state(
        request.app.state.courtier_config.repo_root, scan_skills=False
    ):
        if pkg["name"] == domain_name:
            if not pkg["enabled"]:
                raise HTTPException(400, f"domain package 已禁用: {domain_name}")
            return pkg["skillsPath"]
    raise HTTPException(400, f"未知 domain package: {domain_name}")


# ---- Plugins -----------------------------------------------------------------


def _plugin_items(request: Request) -> list[dict[str, Any]]:
    plugin_system = request.app.state.plugin_system
    status = plugin_system.get_status()
    scan_results = plugin_system.get_scan_results()
    items = []
    for name, result in sorted(scan_results.items()):
        manifest = result.manifest
        proc_status = status.get(name)
        tools = []
        if manifest is not None:
            tools = [
                {
                    "name": t.name,
                    "displayName": t.display_name or t.name,
                    "description": t.description,
                }
                for t in manifest.capabilities.tools
            ]
        items.append(
            {
                "name": name,
                # Domain grouping follows the plugin→domain mapping derived
                # from the directory layout (plugins/shared → "shared",
                # plugins/<domain>/... → <domain>).  Using the immediate
                # parent directory name instead would misgroup plugins under
                # a nested wrapper dir such as plugins/docaudit/audit/.
                "source": plugin_system.plugin_domain(name) or "shared",
                "scanStatus": result.status.value,
                "scanError": result.error,
                "state": proc_status["state"] if proc_status else "NOT_STARTED",
                "version": (proc_status or {}).get(
                    "version", manifest.version if manifest else "unknown"
                ),
                "restartCount": (proc_status or {}).get("restart_count", 0),
                "description": manifest.description if manifest else "",
                "tools": tools,
            }
        )
    return items


@router.get("/plugins")
@limiter.limit("30/minute")
async def list_plugins(request: Request, _: dict = Depends(require_admin)):
    """列出全部插件（含未启动/被阻止）及其运行状态。"""
    return {"items": _plugin_items(request)}


@router.post("/plugins/{name}/action")
@limiter.limit("10/minute")
async def plugin_action(
    name: str,
    request: Request,
    body: dict[str, str],
    _: dict = Depends(require_admin),
):
    """对插件执行 start / stop / restart 操作。"""
    action = body.get("action", "")
    plugin_system = request.app.state.plugin_system
    try:
        if action == "start":
            state = await plugin_system.start_plugin(name)
        elif action == "stop":
            state = await plugin_system.stop_plugin(name)
        elif action == "restart":
            state = await plugin_system.restart_plugin(name)
        else:
            raise HTTPException(400, "action 必须是 start / stop / restart")
    except KeyError:
        raise HTTPException(404, f"插件不存在: {name}")
    return {"name": name, "state": state}


@router.get("/plugins/{name}/logs")
@limiter.limit("30/minute")
async def get_plugin_logs(
    name: str,
    request: Request,
    tail: int = 200,
    _: dict = Depends(require_admin),
):
    """插件日志：独立部署后日志归插件侧（docker logs / 进程日志），主进程不再 tee。"""
    plugin_system = request.app.state.plugin_system
    if name not in plugin_system.get_scan_results():
        raise HTTPException(404, f"插件不存在: {name}")
    raise HTTPException(
        410,
        "插件已独立部署，日志请查看插件所在主机的进程/容器日志"
        "（docker compose logs <插件服务名>）",
    )


# ---- Skills ------------------------------------------------------------------


@router.get("/skills")
@limiter.limit("30/minute")
async def list_skills_handler(request: Request, _: dict = Depends(require_admin)):
    """按 domain package 分组列出全部技能；禁用的域也展示（便于重新启用）。"""
    from ..services.domain_admin_service import list_domains_with_state

    return {"domains": list_domains_with_state(request.app.state.courtier_config.repo_root)}


class DomainCreateRequest(BaseModel):
    name: str = Field(..., max_length=64)
    title: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)
    locale: str = "zh-CN"


@router.post("/domains")
@limiter.limit("5/minute")
async def create_domain_handler(
    body: DomainCreateRequest,
    request: Request,
    _: dict = Depends(require_admin),
):
    """脚手架创建新 domain 包（config/domain.yaml + prompts + skills/）。"""
    from ..services.domain_admin_service import scaffold_domain

    result = scaffold_domain(
        request.app.state.courtier_config.repo_root,
        name=body.name,
        title=body.title.strip(),
        description=body.description.strip(),
        locale=body.locale,
    )
    _rediscover(request)
    return result


class DomainEnabledRequest(BaseModel):
    enabled: bool


@router.post("/domains/{name}/enabled")
@limiter.limit("10/minute")
async def set_domain_enabled_handler(
    name: str,
    body: DomainEnabledRequest,
    request: Request,
    _: dict = Depends(require_admin),
):
    """启用/禁用 domain 包（写入 domains/.disabled 名单并即时重新发现）。"""
    from ..services.domain_admin_service import set_domain_enabled

    set_domain_enabled(request.app.state.courtier_config.repo_root, name, body.enabled)
    _rediscover(request)
    return {"name": name, "enabled": body.enabled}


class SkillEnabledRequest(BaseModel):
    enabled: bool
    domain: str


@router.post("/skills/{name}/enabled")
@limiter.limit("10/minute")
async def set_skill_enabled_handler(
    name: str,
    body: SkillEnabledRequest,
    request: Request,
    _: dict = Depends(require_admin),
):
    """启用/禁用指定 domain 包下的技能（改写 frontmatter 的 enabled 字段）。"""
    set_skill_enabled(_resolve_skills_dir(request, body.domain), name, body.enabled)
    return {"name": name, "enabled": body.enabled, "domain": body.domain}


@router.get("/skills/{name}")
@limiter.limit("30/minute")
async def get_skill_handler(
    name: str,
    request: Request,
    domain: str,
    _: dict = Depends(require_admin),
):
    """返回单个技能的完整详情（含 Markdown 工作流指令），供查看/编辑。"""
    from ..services.skill_admin_service import get_skill

    return get_skill(_resolve_skills_dir(request, domain), name)


class SkillUpdateRequest(BaseModel):
    domain: str = Field(..., description="目标 domain package 名称")
    display_name: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=512)
    mode: str = ""
    default_mode: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""


@router.put("/skills/{name}")
@limiter.limit("10/minute")
async def update_skill_handler(
    name: str,
    body: SkillUpdateRequest,
    request: Request,
    _: dict = Depends(require_admin),
):
    """更新技能的可编辑字段并重写技能文件（name/enabled 不在此修改）。"""
    from ..services.skill_admin_service import update_skill

    known_tools = {t.name for t in request.app.state.tool_registry.list_tools()}
    return update_skill(
        _resolve_skills_dir(request, body.domain),
        name,
        display_name=body.display_name.strip(),
        description=body.description.strip(),
        mode=body.mode,
        default_mode=body.default_mode,
        tools=body.tools,
        skills=body.skills,
        tags=body.tags,
        system_prompt=body.system_prompt,
        known_tools=known_tools,
    )


class SkillCreateRequest(BaseModel):
    name: str = Field(..., max_length=64)
    domain: str = Field(..., description="目标 domain package 名称")
    display_name: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=512)
    mode: str = ""
    default_mode: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    system_prompt: str = ""


@router.post("/skills")
@limiter.limit("10/minute")
async def create_skill_handler(
    body: SkillCreateRequest,
    request: Request,
    _: dict = Depends(require_admin),
):
    """在指定 domain 包的 skills 目录创建新技能。"""
    known_tools = {t.name for t in request.app.state.tool_registry.list_tools()}
    return create_skill(
        _resolve_skills_dir(request, body.domain),
        name=body.name,
        display_name=body.display_name.strip(),
        description=body.description.strip(),
        mode=body.mode,
        default_mode=body.default_mode,
        tools=body.tools,
        skills=body.skills,
        tags=body.tags,
        system_prompt=body.system_prompt,
        known_tools=known_tools,
    )
