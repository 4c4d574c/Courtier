# Tools-to-Plugins Migration Design

**Date:** 2026-06-07
**Status:** Approved
**Author:** Hu Lan

## 1. Overview

将所有工具从进程内直接注册迁移到插件子进程（JSON-RPC over stdio）提供。旧的 Domain Tools 全部废弃，Skills 转换为独立插件子进程。OrchestratorAgent 的子代理从硬编码改为从 PluginSystem 动态发现。

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| 工具提供方式 | 全部通过 plugin 子进程（JSON-RPC over stdio） | 统一架构，完全隔离 |
| Skill 提示词归属 | PluginSystem 管理（插件注册声明携带） | 谁提供工具谁提供提示词 |
| 插件粒度 | 一个 Skill = 一个插件子进程 | 进程数可控，管理简单 |
| 子代理编排 | 保留两级编排，但子代理动态发现 | Orchestrator 灵活调度 |
| 插件暴露形式 | Tool + 可选 Agent 共存 | 支持粗细两种粒度编排 |

## 2. Architecture

### 迁移前

```
OrchestratorAgent
  ├── DOMAIN_TOOLS (直接 import，进程内，6个)
  ├── SubAgentRunner (硬编码 5 个子代理类)
  │     ├── FormatAuditorAgent → skills/format_audit/tools.py
  │     ├── ContentAuditorAgent → skills/content_audit/tools.py
  │     └── ...
  └── ToolRegistry (统一注册但来源混杂)
```

### 迁移后

```
OrchestratorAgent
  ├── ToolRegistry
  │     ├── ProxyTool → plugin:parse (子进程)
  │     ├── ProxyTool → plugin:format_audit/* (子进程)
  │     └── ProxyTool → plugin:content_audit/* (子进程)
  ├── AgentRegistry (动态发现)
  │     ├── ProxyAgent → plugin:format_audit
  │     ├── ProxyAgent → plugin:content_audit
  │     └── ProxyAgent → plugin:plagiarism
  └── PromptPipeline
        └── 插件系统注入的 tool-use 提示词
```

## 3. Plugin Manifest Extension

扩展 `plugin.yaml` 以携带系统提示词和能力绑定关系：

```yaml
# plugins/format_audit/plugin.yaml
name: format_audit
version: "1.0.0"

capabilities:
  tools:
    - name: detect_doc_type
      display_name: 检测文档类型
      description: ...
      parameters: {...}
    - name: audit_format
      ...

  agent:
    name: format_auditor
    display_name: 格式审计器
    role: "你是文档格式审计专家..."
    tools: [detect_doc_type, audit_format, load_format_spec, list_format_rule_types]

  system_prompt: |
    ## 格式审计
    使用格式审计工具验证文档是否符合 GB/T 9704-2012 公文格式规范...
```

### PluginManifest 模型扩展

`src/plugin/manifest.py` 的 `Capabilities` 模型新增 `system_prompt` 字段：

```python
class Capabilities(BaseModel):
    tools: list[ToolCapability] = []
    checkers: list[CheckerCapability] = []
    agents: list[AgentCapability] = []
    routes: list[RouteCapability] = []
    processors: list[ProcessorCapability] = []
    system_prompt: str = ""  # 新增：SKILL.md 正文，注入 PromptPipeline
```

### system_prompt 注入点

`ExtensionRegistry.on_register()` 处理 `system_prompt`，通过 `PromptPipeline.add_section()` 注入到 Agent 的提示词流水线中：

```
PluginManager 启动子进程
  → 子进程发送 plugin.register(capabilities: {system_prompt: "..."})
    → ExtensionRegistry.on_register()
      → 创建 ProxyTool/ProxyAgent 注册到对应 Registry
      → 将 system_prompt 存入 PluginSystem 的 prompt 收集器
        → Agent 构建时，PromptPipeline 从 PluginSystem 获取所有插件的 system_prompt
          作为 "PluginTools" 段注入
```

注入时机：Agent 构造时（而非全局注入），不同 Agent 可以有选择地加载不同插件的提示词。

## 4. App Startup Integration

### 启动流程

```python
def create_app(sessions_dir: str = "") -> FastAPI:
    # ... settings, logging ...

    init_checkers()  # 保持不变

    app = FastAPI(...)
    # CORS, shared state ...

    app.state.tool_registry = ToolRegistry()
    app.state.checker_registry = get_checker_registry()

    # PluginSystem 统一接管所有工具注册
    app.state.plugin_system = PluginSystem(
        plugins_dir="plugins",
        tool_registry=app.state.tool_registry,
        checker_registry=app.state.checker_registry,
    )

    # 启动时扫描 plugins/，启动子进程，自动注册 ProxyTool/ProxyAgent
    # (通过 lifespan / startup event 异步调用)
    # await app.state.plugin_system.start()

    # DOMAIN_TOOLS 注册代码删除

    app.include_router(router)
    return app
```

### 关闭流程

```python
@app.on_event("shutdown")
async def shutdown_plugins():
    await app.state.plugin_system.shutdown()
```

### 关键变化

| 项目 | 迁移前 | 迁移后 |
|------|--------|--------|
| 工具注册 | `DOMAIN_TOOLS` 直接 register | PluginSystem 自动 ProxyTool 注册 |
| DOMAIN_TOOLS 列表 | 存在并使用 | 删除 |
| PluginSystem 集成 | 未集成 | `create_app()` 中启动 |

## 5. Domain Tools Migration

### 映射关系

| Domain Tool | 工具名 | 迁移目标 Skill |
|-------------|--------|---------------|
| `ParseDocumentTool` | `parse_document_direct` | → `parse` |
| `FormatCheckTool` | `format_check_direct` | → `format_audit` |
| `ContentComplianceTool` | `content_compliance_direct` | → `content_audit` |
| `TextCorrectionTool` | `text_correction_direct` | → `text_correction` |
| `PlagiarismCheckTool` | `plagiarism_check_direct` | → `plagiarism` |
| `AnnotateTool` | `annotate_direct` | → `annotate` |

### 迁移动作

1. 依次为每个 Skill 创建 `plugin.yaml` + `entry.py`（基于 `PluginRuntime`）
2. 把 Domain Tool 的 `execute()` 逻辑移到插件子进程的 handler 中
3. 删除 `src/agent/tools/domain/` 整个目录
4. 删除 `app.py` 中 `DOMAIN_TOOLS` 的注册代码
5. 删除 `orch.py` 中 `from ..tools.domain import DOMAIN_TOOLS` 和 `tools.extend(DOMAIN_TOOLS)`

## 6. OrchestratorAgent Refactoring

### 子代理发现：硬编码 → 动态

```python
# 迁移前 — 硬编码子代理类型
_SUBAGENTS = [
    ("format_auditor", FormatAuditorAgent, FailureStrategy.STRICT, None),
    ("content_auditor", ContentAuditorAgent, FailureStrategy.TOLERANT, None),
    ...
]

# 迁移后 — 从 PluginSystem 动态获取 ProxyAgent
# Orchestrator 通过 ExtensionRegistry.get_agents() 获取所有可用子代理
# LLM 在运行时决定调用哪个子代理
```

### 关键变化

- `FormatAuditorAgent`、`ContentAuditorAgent` 等类不再需要——插件子进程内已有完整 agent 实现
- Orchestrator 的 `role` 提示词改为动态发现可用审计代理
- `SubAgentRunner` 不变，接收 `SubAgentConfig`，传入 `ProxyAgent` 即可
- `build_tools()` 生成的 `run_*` 工具由插件声明的 agent 列表动态生成

## 7. Skill Plugin Directory Structure

### 迁移前后对比

```
迁移前：src/agent/skills/format_audit/
  ├── SKILL.md              # 前页元数据 + 提示词正文
  ├── tools.py              # ToolProtocol 类
  └── ...

迁移后：plugins/format_audit/
  ├── plugin.yaml           # 能力声明（从 SKILL.md 前页 + tools.py 派生）
  ├── entry.py              # PluginRuntime 子类，注册 handler
  ├── tools.py              # 工具执行逻辑（保留，被 entry.py 导入）
  ├── pyproject.toml        # 插件自己的依赖
  └── ...
```

### entry.py 示例

```python
from src.plugin.sdk import PluginRuntime
from .tools import DetectDocTypeTool, AuditFormatTool, ...

class FormatAuditPlugin(PluginRuntime):
    def register_capabilities(self):
        return [
            {"type": "tool", "name": "detect_doc_type", ...},
            {"type": "tool", "name": "audit_format", ...},
            {"type": "tool", "name": "load_format_spec", ...},
            {"type": "tool", "name": "list_format_rule_types", ...},
            {"type": "agent", "name": "format_auditor", ...},
        ]
    
    def _setup_handlers(self):
        detect = DetectDocTypeTool()
        audit = AuditFormatTool()
        spec = LoadFormatSpecTool()
        list_types = ListFormatRuleTypesTool()
        
        @self.on("tool.execute")
        async def handle_tool_execute(params):
            tool_name = params["tool"]
            args = params.get("args", {})
            handlers = {
                "detect_doc_type": detect.execute,
                "audit_format": audit.execute,
                "load_format_spec": spec.execute,
                "list_format_rule_types": list_types.execute,
            }
            result = await handlers[tool_name](**args)
            return result.model_dump()
        
        @self.on("agent.run")
        async def handle_agent_run(params):
            # 子代理内部运行自己的 agent_loop
            ...
```

## 8. Cleanup After Migration

迁移完成后需清理的旧代码：

| 清理项 | 路径 | 说明 |
|--------|------|------|
| Domain Tools 目录 | `src/agent/tools/domain/` | 整体删除 |
| DOMAIN_TOOLS 导入 | `app.py`、`orch.py` | 删除 import 和注册代码 |
| Skills 目录 | `src/agent/skills/` | 整体删除（内容已迁移到 `plugins/`） |
| SkillLoader | `src/agent/skills/loader.py` | 删除（不再需要加载 SKILL.md 和导入 tools.py） |
| 子代理类文件 | `src/agent/agents/format_auditor.py` 等 | 删除（插件内已有 agent 实现） |
| Agent 导入 | `orch.py` 中的 auditor 导入 | 删除 `_SUBAGENTS` 硬编码和 auditor 导入 |

保留不变：
- `src/agent/tools/registry.py` — 工具注册表继续使用
- `src/agent/tools/protocol.py` — ToolProtocol 不变
- `src/agent/tools/builtin/` — Echo、ListArtifacts、GetArtifact、PersistOutput 保留（它们是 agent 基础设施，不是领域工具）
- `src/agent/agents/subagent/` — SubAgentRunner 保留
- `src/content_compliance/` — 合规检查器保留（不走插件）

## 9. Migration Order

迁移按依赖关系和风险分批进行：

1. **第一批：无内部依赖的简单 Skill**
   - `search`（工具最少，逻辑简单）
   - `parse`（被所有下游依赖，先迁移确保稳定）

2. **第二批：Domain Tools 重复的 Skill**
   - `format_audit` / `annotate` / `text_correction`
   - 迁移后删除对应的 Domain Tool

3. **第三批：复杂 Skill**
   - `content_audit` / `plagiarism` / `style_audit`

4. **最后：辅助 Skill**
   - `template`

每个 Skill 迁移后跑对应测试，确认功能正常再继续下一个。

## 10. Error Handling

### 插件崩溃处理

- ProcessManager 已实现崩溃检测和自动重启（指数退避，最多 3 次）
- 崩溃期间该插件的 ProxyTool/ProxyAgent 从注册表移除（`on_unregister`）
- Agent 调用崩溃插件的工具时收到 `PluginCrashedError`，LLM 可感知并重试或跳过

### 启动失败处理

- `PluginScanner` 返回 BLOCKED 状态的插件不启动
- 部分插件启动失败不影响其他插件和应用整体启动
- 错误日志记录到 audit_log_dir

## 11. Testing Strategy

### 单元测试

- 每个插件的 `entry.py` 的 handler 逻辑（使用 `PluginRuntime` 的 queue 模式注入测试输入）
- `ExtensionRegistry` 的 system_prompt 注入
- `OrchestratorAgent` 的动态 agent 发现

### 集成测试

- `PluginSystem.start()` → 工具在 `ToolRegistry` 中可用
- ProxyTool → JSON-RPC → 插件子进程 → 正确结果返回
- 插件崩溃 → 自动重启 → 功能恢复

### 已有测试

- `tests/plugin/` 下 8 个测试文件覆盖 scanner、manifest、runtime、client、proxies、registry、manager、integration
- 迁移过程中保持这些测试通过
