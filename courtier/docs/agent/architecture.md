# DocAudit 系统架构文档

## 目录

1. [项目概述](#1-项目概述)
2. [系统架构总览](#2-系统架构总览)
3. [核心子系统](#3-核心子系统)
   - [3.1 Agent 子系统](#31-agent-子系统)
   - [3.2 工具系统](#32-工具系统)
   - [3.3 插件系统](#33-插件系统)
     - [3.3.11 工具与子代理的关系](#3311-工具与子代理的关系)
   - [3.4 工件系统](#34-工件系统)
   - [3.5 上下文管理](#35-上下文管理)
   - [3.6 Prompt 系统](#36-prompt-系统)
   - [3.7 Hook 系统](#37-hook-系统)
   - [3.8 权限系统](#38-权限系统)
4. [子代理系统](#4-子代理系统)
5. [业务模块](#5-业务模块)
6. [数据层](#6-数据层)
7. [API 层](#7-api-层)
8. [可观测性](#8-可观测性)
9. [部署架构](#9-部署架构)
10. [核心数据流与运行流程](#10-核心数据流与运行流程)

---

## 1. 项目概述

DocAudit 是一款面向中文党政机关公文的智能审核系统，提供从文件上传、智能解析、多维度审核到批注导出的全流程自动化服务。系统深度集成大语言模型（LLM）、OCR 文字识别、Elasticsearch 全文检索等技术，覆盖**文件智能解析、格式审核、内容审核、文本纠错、行文风格审查、文档查重**六大核心能力。

### 技术栈总览

| 组件 | 技术选型 | 用途 |
|------|----------|------|
| 后端框架 | FastAPI (Python 3.12+) | HTTP API + SSE 流式响应 |
| 数据库 | MySQL 8.x (SQLAlchemy 异步 ORM) | 文档、规则、资源等持久化存储 |
| 搜索引擎 | Elasticsearch 8.x | 全文检索、RAG 知识库 |
| 对象存储 | MinIO (S3 兼容) | 上传文件、解析结果存储 |
| OCR 引擎 | PaddleOCR (PPStructureV3) | 扫描件文字识别与版面分析 |
| 大语言模型 | 通义千问 Qwen3.5-27B / ChineseErrorCorrector3-4B | 智能审核、纠错、对话 |
| 文档处理 | PyMuPDF / python-docx / LibreOffice | PDF/DOCX 解析与生成 |
| 可观测性 | OpenTelemetry + Prometheus + Grafana | 分布式追踪、指标采集 |
| 插件隔离 | 子进程 + JSON-RPC 2.0 over stdio | 插件能力隔离 |
| 包管理 | uv | Python 依赖管理与虚拟环境 |

---

## 2. 系统架构总览

### 2.1 分层架构

系统采用**分层 + 插件化**的架构模式，从上到下分为四层：

```
┌──────────────────────────────────────────────────────┐
│                    API 层                            │
│  FastAPI Routes (sessions, files, control)           │
│  SSE Adapter (流式事件输出)                           │
│  Observability Middleware                            │
├──────────────────────────────────────────────────────┤
│                   服务层                             │
│  AgentService  StreamService  FileService            │
│  SessionService                                     │
├──────────────────────────────────────────────────────┤
│                   核心引擎层                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │  Agent   │ │  Plugin  │ │  Tool    │             │
│  │  Loop    │ │  System  │ │  System  │             │
│  └──────────┘ └──────────┘ └──────────┘             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │ Artifact │ │ Context  │ │  Prompt  │             │
│  │  System  │ │ Manager  │ │ Pipeline │             │
│  └──────────┘ └──────────┘ └──────────┘             │
├──────────────────────────────────────────────────────┤
│                   业务模块层                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │ docparse │ │ docannot │ │ doccorr  │             │
│  └──────────┘ └──────────┘ └──────────┘             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │plagiarism│ │ validator│ │docbuilder│             │
│  └──────────┘ └──────────┘ └──────────┘             │
├──────────────────────────────────────────────────────┤
│                   数据 / 基础设施层                    │
│  MySQL (dbop)  Elasticsearch (es)  MinIO (storage)  │
└──────────────────────────────────────────────────────┘
```

### 2.2 插件化架构

核心引擎通过**插件系统**与业务模块解耦。9 个独立插件运行在子进程中，通过 JSON-RPC 2.0 over stdio 与核心引擎通信：

```
┌─────────────────────────────────────────────────────┐
│                   Host Process (FastAPI)              │
│                                                      │
│  ┌────────────────┐        ┌──────────────────┐     │
│  │OrchestratorAgent│──────▶│  AgentRuntime    │     │
│  └────────────────┘        └──────────────────┘     │
│           │                          │               │
│           ▼                          ▼               │
│  ┌────────────────┐        ┌──────────────────┐     │
│  │  ToolRegistry  │        │ExtensionRegistry │     │
│  │  (ProxyTool)   │◀───────│  (ProxyAgent)    │     │
│  └────────────────┘        └──────────────────┘     │
│           │                          │               │
│           ▼                          ▼               │
│  ┌────────────────────────────────────────────┐     │
│  │           PluginSystem                     │     │
│  │  ┌──────────────┐  ┌──────────────────┐   │     │
│  │  │PluginScanner │  │ ProcessManager   │   │     │
│  │  └──────────────┘  └──────────────────┘   │     │
│  └────────────────────────────────────────────┘     │
│           │                          │               │
├───────────┼──────────────────────────┼───────────────┤
│           │    JSON-RPC over stdio   │               │
│           ▼                          ▼               │
│  ┌────────────────┐        ┌──────────────────┐     │
│  │ parse plugin   │        │format_audit plugin│     │
│  │ (子进程)        │        │ (子进程, LLM Agent)│     │
│  └────────────────┘        └──────────────────┘     │
│  ┌────────────────┐        ┌──────────────────┐     │
│  │text_correction │        │ style_audit      │     │
│  │ plugin         │        │ plugin           │     │
│  └────────────────┘        └──────────────────┘     │
│  ...  (共 9 个插件子进程)                             │
└─────────────────────────────────────────────────────┘
```

---

## 3. 核心子系统

### 3.1 Agent 子系统

Agent 子系统是实现 LLM 驱动自动化的核心，位于 `src/agent/`。核心设计遵循 **Think-Act-Observe 循环**模式。

#### 3.1.1 Agent 基类

**文件**: `src/agent/agents/base.py`

```python
class Agent:
    """AI Agent 基类，封装 LLM 驱动的任务执行能力。"""
    name: str          # 标识符
    role: str          # 角色描述（写入 system prompt）
    tools: list        # 可用工具列表
    model: ModelClient  # LLM 客户端
    memory: MemoryStore  # 持久化记忆
    hooks: HookChain    # 生命周期钩子
    permissions: PermissionGate  # 权限控制
```

`Agent.run(task, options)` 为入口方法，内部流程：
1. 构建 system prompt（角色 + 工具声明 + 插件系统 prompt）
2. 初始化 `AgentState`
3. 调用 `agent_loop()` 执行 Think-Act-Observe 循环
4. 记录指标（Prometheus metrics）
5. 返回 `AgentResult(status, content, data, tool_results, termination_reason, final_state)`

**关键设计**：支持增量工具同步。插件工具可在 Agent 构造完成后动态注册，每次 `run()` 调用时通过 `_sync_tools()` 发现新工具。

#### 3.1.2 Agent 主循环

**文件**: `src/agent/core/loop.py`

`agent_loop()` 是系统中最核心的不变量——无论 Agent 规模如何增长，该循环保持简单稳定。

**状态机流转**：

```
idle → thinking → waiting_for_tool → observing → completed
                 ↘ blocked（权限拒绝）
                 ↘ error（模型错误/无工具注册表）
```

**循环体**（每轮迭代）：

```
while not state.is_terminal():
    1. pre_think hook ← 钩子：思考前处理
    2. THINK: think_phase() ← 调用 LLM，返回文本或 tool_calls
       ├─ 模型返回文本无工具调用 → state = completed（终态）
       └─ 模型返回 tool_calls → state = waiting_for_tool
    3. GATE: 权限检查 ← 逐个 tool_call 校验 PermissionGate
       └─ 检查失败 → state = blocked
    4. ACT + OBSERVE: execute_tools_phase()
       ├─ 并行执行所有工具调用
       ├─ 持久化大型输出到 CacheStore（$ref 替换）
       ├─ 注册 Artifact 到 ArtifactStore
       └─ 触发 on_tool_result 回调
    5. 护栏检查 (Loop Guards):
       ├─ Business Artifact Progress ← 无业务产出则终止
       ├─ Terminal Tool Readiness ← 就绪则注入提示; 阻塞则强制终止
       ├─ Explore Loop Detection ← 检测无意义的探索循环
       └─ Hint Injection ← 注入上下文提示
    6. 写入审计日志
    7. Layer 2 微压缩（旧工具结果替换为占位符）
    8. post_observe hook ← 钩子：观察后处理
```

**护栏机制详解**：

| 护栏 | 文件 | 功能 |
|------|------|------|
| Business Artifact Progress | `loop_guards.py` | 跟踪非 debug artifact 的产出，连续多轮无新产出则终止 |
| Explore Loop Detection | `loop_guards.py` | 检测连续只读工具调用（探索性循环），连续 N 轮仅有读操作 + null 结果则终止 |
| Hint Injection | `loop_hints.py` | 当 terminal tool 就绪时，注入可执行工具摘要；当工具因输入不足被阻塞时，注入缺失提示 |
| Max Steps | 内置 | 最大轮次限制（默认 20），防止无限循环 |

**OpenTelemetry 集成**：每个 Agent 运行创建一个 `agent_span`，每轮 LLM 调用创建 `llm_span`，每个工具调用创建 `tool_span`，形成完整调用链。

#### 3.1.3 Agent 状态机

**文件**: `src/agent/core/state.py`

`AgentState` 是**不可变的 Pydantic BaseModel**（`frozen=True`），每次状态变更返回全新的实例：

```python
class AgentState(BaseModel, frozen=True):
    status: AgentStatus     # idle|thinking|waiting_for_tool|observing|completed|blocked|error
    messages: tuple[Message, ...]  # 对话历史（不可变元组）
    current_step: int       # 当前迭代轮次
    tool_calls: tuple[ToolCall, ...]  # 本轮待执行的工具调用
    tool_results: tuple[ToolResult, ...]  # 上一轮工具执行结果
    max_steps: int          # 最大迭代轮次（默认 20）
    termination_reason: str | None  # 终止原因
```

**关键设计决策**：使用不可变模式（frozen dataclass/Pydantic model），避免并发场景下的状态腐败。每次 `add_thought()` 和 `add_observation()` 创建新实例。

**Message 不可变模型**：`@dataclass(frozen=True)` 的 Message 对象支持 OpenAI 兼容格式的序列化。

#### 3.1.4 OrchestratorAgent（编排代理）

**文件**: `src/agent/agents/orch.py`

顶层编排代理，负责统筹整个审核流水线：

1. **初始化阶段**：
   - 从 `SkillRegistry` 读取已编译的 Skill 目录，为每个启用的 Skill 构建 `SkillTool` 并注册到 `ToolRegistry`
   - 通过 `build_catalog()` 把可用 Skill 列表注入系统提示词
   - 从 `ToolRegistry` 发现所有工具（内置 + 插件提供的 ProxyTool + SkillTool）
   - 构建系统提示词（角色 + Skill 目录 + 工作流规则）
   - 持有 `AgentRuntime` 引用，用于实际派生子代理

2. **运行阶段**（LLM 驱动）：
   ```
   用户任务 → 解析文档 → 格式审核（政府文档）→ 内容审核 → 文本纠错
            → 行文风格审查 → 文档查重
   ```
   工作流顺序由系统提示词中编码的规则决定。

#### 3.1.5 Model Client

**文件**: `src/agent/core/model.py`

LLM 调用的统一抽象：

```python
class ModelResponse:
    content: str | None          # 文本响应
    tool_calls: list[ToolCall]   # 工具调用
    usage: dict                  # token 用量
    finish_reason: str           # 终止原因

class ModelClient(Protocol):
    model_name: str
    async def generate(messages, tools, temperature, max_tokens) -> ModelResponse: ...

class OpenAIModelClient(ModelClient):
    """OpenAI 兼容 API 客户端（支持通义千问等）。"""
    
class MockModelClient(ModelClient):
    """测试用 Mock 客户端。"""
```

`OpenAIModelClient` 通过 `AsyncOpenAI` SDK 调用 LLM，支持：
- 流式响应（streaming token delivery）
- 推理内容分离（`reasoning_content` 字段）
- 工具调用（function calling）
- 可配置的 temperature、max_tokens、extra_body

---

### 3.2 工具系统

工具系统定义了 Agent 如何与外部世界交互的接口规范。

#### 3.2.1 ToolProtocol

**文件**: `src/agent/tools/protocol.py`

```python
class ToolProtocol(Protocol):
    name: str            # 工具唯一标识
    description: str     # 工具描述（LLM 可见）
    parameters: dict     # JSON Schema（LLM 参数验证）
    
    async def execute(**kwargs) -> ToolResult: ...
    def summarize(result: ToolResult) -> ToolSummary: ...

class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = {}
```

**扩展协议**（通过运行时 Protocol 检查组合）：

| 协议 | 用途 |
|------|------|
| `ToolWithContracts` | 声明输入/输出 Artifact 契约（input_fields, output_artifact_type, output_schema） |
| `ToolWithRuntimePolicy` | 声明运行时策略（skip_persist, runtime_policy） |
| `ToolWithDisplay` | 声明显示元数据 |

#### 3.2.2 ToolRegistry

**文件**: `src/agent/tools/registry.py`

统一工具注册表，支持：
- 注册/注销/枚举工具
- `get(name)` 按名称查找
- `execute(name, **params)` 执行并记录指标
- `get_schemas()` 返回 OpenAI 兼容的工具声明列表
- `list_tools()` 按 display_name 排序返回工具信息
- **运行时状态重置**：每轮 Agent 循环开始前重置 per-run 计数器

#### 3.2.3 内置工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `echo` | `builtin/echo.py` | 回显工具（测试用） |
| `get_artifact` | `builtin/get_artifact.py` | 按 artifact ID 获取已注册的工件 |
| `list_artifacts` | `builtin/list_artifacts.py` | 列出当前会话的工件清单 |
| `persist_output` | `builtin/persist_output.py` | 持久化产出物到文件 |

---

### 3.3 插件系统

插件系统是整个架构的核心基础设施，实现了**子进程隔离**和**JSON-RPC 通信**的插件机制。

#### 3.3.1 设计目标

- **隔离性**：每个插件运行在独立子进程中，崩溃不影响主进程
- **语言无关性**：通过 JSON-RPC 2.0 over stdio 通信，理论上支持任何语言实现插件
- **热注册**：插件启动后动态注册能力到主机注册表中
- **自动恢复**：插件崩溃后自动重启（带指数退避）
- **分布式追踪**：支持跨进程 OpenTelemetry 追踪

#### 3.3.2 架构层次

```
PluginSystem (顶层编排器)
├── PluginScanner    (扫描 plugins/ 目录，验证 plugin.yaml)
├── ProcessManager   (子进程生命周期管理)
│   └── JSONRPCClient (每个子进程一个客户端)
└── ExtensionRegistry (路由能力到主机注册表)
    ├── ProxyTool    (工具代理 → ToolRegistry)
    ├── ProxyChecker (检查器代理 → CheckerRegistry)
    ├── ProxyAgent   (代理代理 → AgentRuntime / 子代理)
    └── ProxyRoute   (路由代理 → FastAPI)
```

#### 3.3.3 PluginSystem（顶层编排器）

**文件**: `src/plugin/__init__.py`

```python
class PluginSystem:
    def __init__(self, plugins_dir, tool_registry, checker_registry):
        ...
    
    async def start() -> None:
        """扫描插件目录，启动所有有效插件。"""
        # 1. PluginScanner.scan() → 读取并验证所有 plugin.yaml
        # 2. ProcessManager.start(valid_results) → 逐个启动子进程
        # 3. 等待每个插件的 plugin.register 通知
        
    async def shutdown() -> None:
        """优雅关闭所有插件子进程。"""
        
    def get_agents() -> dict[str, ProxyAgent]:
        """返回所有已注册的插件代理。"""
        
    def get_system_prompts() -> str:
        """返回所有插件的系统提示词合并。"""
```

**启动流程**：

```
PluginSystem.start()
  ├─ PluginScanner.scan()
  │   ├─ 遍历 plugins/ 子目录
  │   ├─ 读取 plugin.yaml
  │   ├─ YAML 语法验证
  │   ├─ Pydantic 模型验证 (PluginManifest)
  │   ├─ API 版本兼容性检查 (semver)
  │   └─ 返回 List[PluginScanResult] (status: VALID|BLOCKED)
  │
  └─ ProcessManager.start(valid_results)
      └─ for each valid plugin:
          └─ _start_one(scan_result)
              ├─ uv run <entry.py> (PYTHONPATH=项目根, CWD=插件目录)
              ├─ 等待 plugin.register 通知
              ├─ ExtensionRegistry.on_register(capabilities)
              │   ├─ 类型: tool → ProxyTool → ToolRegistry.register()
              │   ├─ 类型: checker → ProxyChecker → CheckerRegistry.register()
              │   ├─ 类型: agent → ProxyAgent → 存入 AgentRegistry
              │   ├─ 类型: route → ProxyRoute → 存入 RouteRegistry
              │   └─ 类型: system_prompt → 存入提示词列表
              └─ 状态: ACTIVE
```

#### 3.3.4 PluginScanner（插件扫描器）

**文件**: `src/plugin/scanner.py`

扫描 `plugins/` 目录，读取并验证 `plugin.yaml`：

```python
class PluginScanner:
    def scan(plugins_dir) -> list[PluginScanResult]:
        """扫描所有插件目录，返回验证结果列表。"""
        # 1. 遍历 plugins_dir 下所有子目录
        # 2. 查找 plugin.yaml 文件
        # 3. YAML 解析 + Pydantic 验证 (PluginManifest)
        # 4. API 版本兼容性检查 (semver 比较)
        # 5. 返回 ScanResult (VALID 或 BLOCKED + 原因)
```

#### 3.3.5 PluginManifest（插件清单）

**文件**: `src/plugin/manifest.py`

每个插件的 `plugin.yaml` 结构（`capabilities` 为结构化对象，含 `tools` / `agents` / `system_prompt`）：

```yaml
name: format_audit
version: "1.0.0"
api: "1.0"
description: "Format auditing against GB/T 9704-2012"

dependencies:
  host_services:
    - cache
  permissions:
    - read:cache
    - write:cache

capabilities:
  tools:
    - name: detect_document_type
      display_name: 检测文档类型
      description: "Detect Chinese government document type from title and body."
    - name: audit_format
      display_name: 格式审计
      description: "Audit formatting compliance against GB/T 9704-2012."
  # agents: 仍受 schema 支持（ProxyAgent），但当前所有插件仅声明 tools；
  #         审核子代理已迁移至 skills/*.md，由 AgentRuntime 调起执行。
  system_prompt: |
    # 格式审核
    检查公文是否符合 GB/T 9704-2012《党政机关公文格式》国家标准。
```

#### 3.3.6 ProcessManager（进程管理器）

**文件**: `src/plugin/manager.py`

管理所有插件子进程的生命周期：

**状态机**：
```
SCANNED → LOADING → REGISTERING → ACTIVE
                                 → CRASHED → RESTARTING → REGISTERING → ACTIVE
                                 → CRASHED → FATAL (5s 内崩溃)
                                 → STOPPING → STOPPED
```

**关键机制**：

| 机制 | 实现 |
|------|------|
| 子进程启动 | `asyncio.create_subprocess_exec("uv", "run", entry)` |
| 环境变量 | 设置 `PYTHONPATH`（项目根路径）、`DOCAUDIT_PROJECT_ROOT`、解析 `${ENV:VAR}` |
| 健康检查 | 每 30 秒发送 `plugin.health` RPC |
| 崩溃恢复 | 最多重启 3 次，指数退避（1s, 2s, 4s, 上限 30s） |
| 熔断器 | 启动后 5s 内崩溃 → FATAL（不重启） |
| 优雅关闭 | 发送 SIGTERM → 等待 5s → SIGKILL |
| Stderr 监控 | 捕获子进程 stderr，检测崩溃并转发日志 |

#### 3.3.7 JSONRPCClient（通信客户端）

**文件**: `src/plugin/client.py`

每个插件子进程对应一个 `JSONRPCClient` 实例，管理 JSON-RPC 2.0 over stdio 通信：

**通信机制**：
- **传输层**：stdin/stdout，换行符分隔的 JSON
- **消息类型**：Request（有 id）、Response（有 id + result）、Notification（无 id）
- **流式消息**：`JSONRPCStreamChunk`（id + chunk + status: "continue"|"end"）

**核心方法**：

```python
class JSONRPCClient:
    async def call(method, params, timeout) -> dict:
        """请求-响应模式。返回结果 dict。"""
        
    async def stream(method, params, timeout) -> AsyncIterator[JSONRPCStreamChunk]:
        """流式模式。返回异步迭代器，逐 chunk 输出。"""
        
    async def notify(method, params) -> None:
        """通知模式（fire-and-forget）。"""
        
    async def cancel_pending() -> None:
        """取消所有进行中的请求（发送 request.cancel 通知）。"""
```

**异步读取循环**：后台 `asyncio.Task` 持续从子进程 stdout 读取 JSON 行，根据 `id` 分发到对应的 pending Future。

**流式协议**：长运行操作（如 `agent.run`）：
```
→ Request { "id": 1, "method": "agent.run", "params": {...} }
← StreamChunk { "id": 1, "chunk": {...}, "status": "continue", "kind": "token" }
← StreamChunk { "id": 1, "chunk": {...}, "status": "continue", "kind": "think" }
← StreamChunk { "id": 1, "chunk": {...}, "status": "continue", "kind": "tool_result" }
← StreamChunk { "id": 1, "chunk": {...}, "status": "end", "kind": "result" }
```

#### 3.3.8 PluginRuntime（插件 SDK）

**文件**: `src/plugin/sdk/runtime.py`

插件开发者继承 `PluginRuntime` 来构建插件入口点：

```python
class PluginRuntime:
    """插件子进程入口基类。"""
    
    def on(method: str):
        """装饰器：注册 JSON-RPC 方法处理器。
        
        处理器可以是：
        1. 同步/异步函数 → 返回结果 dict（用于 tool.execute, checker.check）
        2. 异步生成器 → 流式输出 chunks（用于 agent.run）
        """
    
    async def run():
        """启动 JSON-RPC 服务循环（stdin → 分发 → stdout）。"""
        
    def register_tool(tool_instance):
        """注册工具实例，自动提取契约元数据。"""
        
    async def run_agent(task, tools, model_config, ...):
        """运行 LLM Agent 循环（带流式输出）。
        
        支持：多轮对话、工具调用、推理内容分离、令牌流式输出。
        """
```

**并发处理**：长运行的 `tool.execute` 请求在 `ThreadPoolExecutor` 中执行，避免阻塞并发的 `agent.run` 流式请求。

**请求取消**：收到 `request.cancel` 通知时，取消活跃的 asyncio Task。

#### 3.3.9 Proxy 对象

**文件**: `src/plugin/proxies.py`

| Proxy 类 | 实现接口 | 功能 |
|----------|----------|------|
| `ProxyTool` | `ToolProtocol` | `execute()` → JSON-RPC `tool.execute`，自动提取契约字段 |
| `ProxyChecker` | ContentChecker Protocol | `check()` → JSON-RPC `checker.check` |
| `ProxyAgent` | Agent-like | `run()` → JSON-RPC streaming `agent.run`，处理 `SubAgentStreamEvent` |
| `ProxyRoute` | Placeholder | HTTP 路由转发 |

**ProxyAgent 的关键特性**：
- 通过流式 JSON-RPC 接收子代理的实时事件
- 支持 OpenTelemetry 分布式追踪上下文传播
- 返回标准化的 `AgentResult`

#### 3.3.10 插件列表

| 插件 | 目录 | 提供能力 |
|------|------|----------|
| parse | `plugins/common/parse/` | Tool: `parse_document` |
| search | `plugins/common/search/` | Tool: `search_documents` |
| annotate | `plugins/common/annotate/` | Tool: `annotate_document` |
| template | `plugins/common/template/` | Tool: `load_template` |
| format_audit | `plugins/audit/format_audit/` | Tools: `detect_document_type`, `audit_format`, `list_format_rule_types` |
| content_audit | `plugins/audit/content_audit/` | Tools: `audit_content` |
| style_audit | `plugins/audit/style_audit/` | Tools: `audit_writing_style`, `list_writing_style_types` |
| text_correction | `plugins/audit/text_correction/` | Tools: `correct_text` |
| plagiarism | `plugins/audit/plagiarism/` | Tools: `detect_plagiarism` |

子代理（原插件中的 `type: agent`）已迁移至 `skills/` 目录，由 `AgentRuntime` 调起执行。Skills 列表见 [`docs/architecture/plugin-skill-boundary.md`](../architecture/plugin-skill-boundary.md)。

#### 3.3.11 工具、子代理与 Skill 的关系

2026-06 重组后，能力分为三层：**Tool（原子工具）**、**SubAgent（通用 Agent）**、**Skill（`.md` 流程定义）**。
插件**只提供 Tool**；业务审核流程改为 Skill，由 `SkillTool` 触发、`AgentRuntime` 创建子代理并执行。

| 维度 | Tool | SubAgent | Skill |
|------|------|----------|-------|
| 定义方式 | 插件 `plugin.yaml` 的 `capabilities.tools` | 无独立定义（由 Skill 动态生成的 `Agent`） | `skills/*.md`（frontmatter + 正文） |
| 主机侧创建 | `ProxyTool` | `AgentRuntime` 运行时创建 | 启动时 `SkillRegistry` 预编译 `SkillConfig` |
| LLM 看到的名字 | `audit_format`、`parse_document` | 不直接可见（被 SkillTool 包裹） | `format_audit(task=...)` |
| RPC / 执行 | `tool.execute` | 通用 `agent_loop` | = 一个 SubAgent 的配置来源 |
| 业务专家可改 | 否 | 否 | **是**（编辑 `.md`） |

> 历史变更：审核子代理（原插件 `type: agent`，曾表现为 `run_format_auditor` 等）已全部迁移为 Skill。
> 内置 `transform` 子代理与 `run_transform` 工具已移除；结果汇总由 OrchestratorAgent 直接处理。

#### 调用链路

**普通工具**（以 `parse_document` 为例）：

```
LLM → parse_document
  └── ProxyTool.execute()
        └── JSON-RPC tool.execute → parse plugin → 返回 ToolResult
```

**Skill**（以 `format_audit` 为例）：

```
LLM → format_audit(task="...")
  └── SkillTool.execute()
        ├── SkillRegistry.get("format_audit") → SkillConfig
        ├── AgentRuntime.spawn() 校验预算/深度/循环
        ├── 从 ToolRegistry 解析 frontmatter 声明的工具
        ├── 若声明 input_model：校验/构建结构化输入
        └── AgentRuntime.delegate() 运行通用 Agent（role = Skill 正文，tools = 声明工具）
              └── 子代理 agent_loop：THINK → 调用 parse_document/audit_format → 返回 AgentResult
        └── 统一为 ExecutionResult（metadata: is_subagent_result、skill 名）
```

`OrchestratorAgent` 初始化时调用 `SkillRegistry.build_catalog()`，把可用 Skill 目录注入 system prompt，
LLM 据此决定调用哪个 Skill 工具。

#### 何时用 Tool，何时定义 Skill

**优先用 Tool（插件）**：
- 功能是确定性的，无需 LLM 推理
- 单次调用即可完成，输入输出能用单一 JSON Schema 描述

**优先定义 Skill（`.md`）**：
- 需要 LLM 多步规划、内部组合多个工具
- 任务边界较宽（如"完整审核一篇文档"）
- 希望由业务专家维护流程，而非修改代码

#### 相关源码

- `src/agent/skills/registry.py`：`SkillRegistry`、`build_catalog()`
- `src/agent/tools/builtin/skill.py`：`SkillTool`
- `src/agent/runtime/`：`AgentRuntime`、子代理预算与生命周期管理
- `src/agent/skills/config.py`：`SkillConfig`
- `skills/*.md`、`skills/schemas/*.py`：Skill 定义与输入模型
- `src/agent/runtime/runtime.py`：`AgentRuntime`（子代理创建、调度、预算与事件桥接）
- `src/plugin/proxies.py`：`ProxyTool` 实现
- `src/plugin/scanner.py`：`PluginScanner`（支持嵌套插件目录）

更详细的概念说明参见 [`docs/architecture/plugin-skill-boundary.md`](../architecture/plugin-skill-boundary.md)。

---

### 3.4 工件系统

工件系统定义了 Agent 循环中组件之间传递数据的类型化契约。

#### 3.4.1 核心概念

**文件**: `src/agent/artifacts/models.py`

```python
class Artifact(BaseModel):
    """类型化的数据工件。"""
    id: str                         # 唯一标识符
    type: str                       # 工件类型（如 "parsed_document", "audit_result"）
    data: Any                       # 工件数据
    metadata: dict                  # 元数据

class ArtifactSchema(BaseModel):
    """工件模式定义。"""
    type: str
    fields: dict[str, InputField]   # 字段定义

class InputField(BaseModel):
    """工件字段定义。"""
    description: str
    type: str                       # JSON type
    required: bool = True
    bindable_from: list[str] | None  # 可从哪些工件类型自动绑定

class RuntimePolicy(BaseModel):
    """工具运行时策略。"""
    terminal: bool = False          # 是否为终端工具（调用后表明任务完成）
    skip_persist: bool = False      # 是否跳过缓存持久化
    read_only: bool = False         # 是否为只读工具
```

#### 3.4.2 ArtifactStore

**文件**: `src/agent/artifacts/store.py`

```python
class ArtifactStore:
    """会话级工件注册表。"""
    
    async def register(artifact: Artifact) -> str:
        """注册工件，返回 ID。"""
        
    def get(id: str) -> Artifact | None:
        """按 ID 获取工件。"""
        
    def list_by_type(type: str) -> list[Artifact]:
        """按类型列出工件。"""
```

`ScopedArtifactStore` 提供过滤视图，用于子代理隔离（排除 debug 类型工件）。

#### 3.4.3 ContractBinder

**文件**: `src/agent/artifacts/binder.py`

自动将 `ArtifactStore` 中的工件绑定到工具的输入参数：

1. 工具声明 `input_fields`（通过 `ToolWithContracts` 协议）
2. 每个字段标记 `bindable_from`（可绑定工件类型列表）
3. `ContractBinder.resolve()` 尝试从 `ArtifactStore` 中自动填充参数
4. 减少 LLM 需手动传递的数据量

#### 3.4.4 投影系统

**文件**: `src/agent/artifacts/{projectors,resolver,executor}.py`

类型投影系统允许将一种工件类型转换为另一种：

- `ProjectorRegistry`：注册类型间的投影函数
- `ProjectionResolver`：解析投影路径
- `ProjectionExecutor`：执行投影链

---

### 3.5 上下文管理

**文件**: `src/agent/core/context_manager.py`

#### 三层上下文预算控制

上下文窗口是所有 LLM Agent 系统的有限资源。`ContextManager` 实现了三层预算控制策略：

```
Layer 1: 大型工具输出持久化
  超过 3,000 字符的工具结果 → 写入磁盘 (CacheStore)，上下文中仅保留 $ref 指针

Layer 2: 旧工具结果微压缩
  保留最近 5 个完整工具结果 → 更早的结果替换为占位符摘要

Layer 3: 全量压缩
  上下文超过 40,000 字符 → LLM 驱动的摘要压缩 → 目标 25,000 字符
```

**大输出阈值常量**：
- `LARGE_OUTPUT_THRESHOLD = 3,000` 字符
- `MAX_CONTEXT_CHARS = 40,000` 字符
- `MAX_CONTEXT_CHARS_AFTER_COMPACT = 25,000` 字符
- `RECENT_TOOL_RESULTS = 5` 个

**$ref 系统**：

大型数据在组件间通过 `$ref` 引用传递：
```
工具 A 产生大型输出 → CacheStore.persist() → 返回 "$ref:parse_document:1"
                                                   ↓
LLM 将 "$ref:parse_document:1" 传递给工具 B → CacheStore.resolve_refs() → 加载真实数据
```

CacheStore 独立于 ContextManager，可被 ToolRegistry 直接使用。

---

### 3.6 Prompt 系统

**文件**: `src/agent/prompts/pipeline.py`

`PromptPipeline` 采用**六段式 s10 模式**构建系统提示词：

```
1. ROLE（角色定义）
2. TOOLS（工具声明）
3. WORKFLOW（工作流规则）
4. ARTIFACTS（工件说明）
5. GUIDELINES（行为指南）
6. OUTPUT（输出格式要求）
```

各段由独立的 `SectionBuilder` 构建，支持灵活组合。

**插件提示词注入**：通过 `PluginSystem.get_system_prompts()` 收集所有插件的提示词贡献，合并到最终的系统提示词中。

---

### 3.7 Hook 系统

**文件**: `src/agent/hooks/chain.py`

Hook 系统是 Agent 主循环的可编程拦截层，让外部代码在不修改核心引擎的前提下，在 Agent 运行时的关键节点插入自定义逻辑。

```python
class HookChain:
    """观察者/拦截器链。钩子可修改 AgentState 并决定流程走向。"""

    def register(
        self, event: str, handler: HookHandler,
        *, priority: int = 0, timeout: float | None = None,
    ) -> HookHandle: ...

    def register_observer(self, event: str, handler: HookObserver) -> HookHandle: ...

    async def run(self, event: str, state: AgentState, *, continue_on_error: bool | None = None) -> AgentState:
        """按优先级执行所有拦截器，再执行所有观察者。"""
```

#### 3.7.1 Handler 类型

系统支持两种 handler：

| 类型 | 签名 | 返回值 | 执行方式 | 用途 |
|---|---|---|---|---|
| **Interceptor**（拦截器） | `async (ctx: HookContext) -> AgentState` | 返回新 `AgentState` | 按 `priority` pipeline 执行 | 修改状态、安全护栏、上下文注入 |
| **Observer**（观察者） | `async (ctx: HookContext) -> None` | 无 | 按注册顺序 fire-and-forget | 日志、审计、指标、查询改写 |

```python
@dataclass(frozen=True)
class HookContext:
    event: str                       # 触发的事件名
    state: AgentState                # 当前 Agent 状态（不可变）
    agent_name: str                  # 当前 Agent 名称
    session_id: str                  # 会话 ID
    current_step: int                # 当前步数
    metadata: dict[str, Any]         # 自定义元数据
```

`HookChain.set_context(agent_name=..., session_id=...)` 在 Agent 启动时注入会话级上下文，所有 handler 通过 `HookContext` 访问。

#### 3.7.2 注册与调度

```python
from src.agent.hooks import HookChain, PRE_THINK, POST_OBSERVE

chain = HookChain(handler_timeout=10.0)  # 链级 handler 10s 超时

# 拦截器：修改状态
chain.register(PRE_THINK, security_guard, priority=100, timeout=5.0)

# 观察者：纯副作用
chain.register_observer(POST_OBSERVE, audit_logger)
```

调度规则：
- 同事件的所有 **Interceptor** 按 `priority` 降序执行，同 priority 保持注册顺序
- 每个 Interceptor 接收上一个 handler 返回的 `AgentState`（pipeline 模式）
- 所有 Interceptor 执行完毕后，再执行该事件的 **Observer**
- Observer 的返回值被忽略，不能修改状态

#### 3.7.3 可拦截的事件

| 事件常量 | 字符串 | 触发位置 | 触发时机 |
|---|---|---|---|
| `PRE_THINK` | `"pre_think"` | `agent_loop` | 每轮循环开始，Think 阶段之前 |
| `POST_OBSERVE` | `"post_observe"` | `agent_loop` | 工具执行 + 观察 + 上下文压缩之后 |
| `PRE_SEARCH` | `"pre_search"` | `AssistantAgent.chat_stream` | ES/RAG 搜索之前 |

`PRE_THINK` 是进行安全护栏和上下文注入的关键点；如果 handler 将 `state.status` 改为 `completed`/`blocked`/`error`，主循环会立即终止。

#### 3.7.4 错误处理与超时

- **默认策略**：handler 异常被捕获并记录到日志，不影响后续 handler（`continue_on_error=True`）
- **严格模式**：`HookChain(continue_on_error=False)` 或单次 `run(..., continue_on_error=False)` 让异常向上传播
- **Observer 异常**：始终被捕获并记录，不影响主循环
- **超时控制**：支持链级 `handler_timeout` 和单 handler 的 `timeout` 参数，使用 `asyncio.wait_for`；超时按 `continue_on_error` 策略处理

#### 3.7.5 可取消注册

```python
handle = chain.register(PRE_THINK, my_handler)
# 后续按需移除
handle.cancel()  # 幂等
```

`register()` 和 `register_observer()` 都返回一个 `HookHandle`，调用 `.cancel()` 可以从链中移除对应的 handler。

#### 3.7.6 使用示例

```python
async def guard_max_steps(ctx: HookContext) -> AgentState:
    """步数过多时强制终止。"""
    if ctx.current_step >= 50:
        return ctx.state.model_copy(update={
            "status": "completed",
            "termination_reason": "guard: max steps reached",
        })
    return ctx.state

async def audit_observer(ctx: HookContext) -> None:
    """记录每轮观察阶段的状态。"""
    logger.info(
        "agent=%s session=%s step=%d status=%s",
        ctx.agent_name, ctx.session_id, ctx.current_step, ctx.state.status,
    )

chain = HookChain()
chain.register(PRE_THINK, guard_max_steps, priority=100)
chain.register_observer(POST_OBSERVE, audit_observer)

agent = Agent(..., hooks=chain)
```

更详细的使用说明见 [`docs/agent/modules/hook-system-guide.md`](./modules/hook-system-guide.md)。

---

### 3.8 权限系统

**文件**: `src/agent/permissions/gate.py`

```python
class PermissionGate:
    def allow(tool_call: ToolCall) -> bool:
        """检查工具调用是否被允许。"""
```

在 Agent 主循环的 GATE 阶段，每个 LLM 请求的工具调用都会通过 `PermissionGate.allow()` 校验。被拒绝的工具调用会立即终止 Agent 循环（状态设为 `blocked`）。

---

## 4. 子代理运行时（AgentRuntime）

子代理系统让 OrchestratorAgent 把子任务委派给一个拥有独立 Think-Act-Observe 循环的子代理。业务审核子代理已迁移为 Skill 机制（见 §3.3.11 与 `SkillTool`）；运行时统一由 `AgentRuntime` 管理，`SubAgentRunner` 与 `_SubAgentTool` 已移除。

#### 4.1 AgentRuntime

**文件**: `src/agent/runtime/runtime.py`

```python
class AgentRuntime:
    def spawn(name: str, task: str, parent_handle: AgentHandle | None,
              context_mode: str = "blackbox", ...) -> AgentHandle:
        """创建子代理句柄，校验预算、深度与循环。"""

    async def delegate(handle: AgentHandle) -> ExecutionResult:
        """运行子代理并返回统一 ExecutionResult。"""

    def terminate(handle: AgentHandle) -> None:
        """结束子代理生命周期。"""
```

**关键设计**：
- **AgentHandle**：不可变子代理句柄，持有任务、预算、循环链、回调与独立 `ArtifactStore`。
- **AgentRuntimeBudget**：组合预算（深度、总 spawn 数、单次/累计运行时间、最大轮数），父句柄分配子句柄预算。
- **Cycle detection**：`agent_chain` 记录调用链，重复 agent 名拒绝 spawn。
- **事件桥接**：子代理事件转换为 `SubAgentStreamEvent`（`scope: "subagent"`）转发到 `SSEAdapter`。
- **结果统一**：子代理 `AgentResult` 经 `ResultSummarizer` 转为 `ExecutionResult`；大结果写入 `ResultStore`（ES 优先，磁盘兜底）。

#### 4.2 SkillTool

**文件**: `src/agent/tools/builtin/skill.py`

`SkillTool` 是 `SkillConfig` 到 `ToolProtocol` 的适配器，使每个 Skill 对 LLM 表现为一个以其 Skill 名命名的可调用工具：

```python
class SkillTool:
    name: str = "format_audit"  # SkillConfig.name
    description: str            # 从 SkillConfig 生成
    parameters: dict            # JSON Schema（task + 可选结构化输入）

    async def execute(**kwargs) -> ExecutionResult:
        # 1. 从 SkillRegistry 取 SkillConfig
        # 2. AgentRuntime.spawn() 创建子代理
        # 3. AgentRuntime.delegate() 执行
        # 4. 合并 skill 元数据后返回 ExecutionResult
```

---

## 5. 业务模块

### 5.1 文档解析 (docparse)

**文件**: `src/docparse/`

支持三种输入格式的智能解析：

| 输入类型 | 处理方法 | 核心技术 |
|----------|----------|----------|
| DOCX | `docx_parser.py` | python-docx 直接读取结构 |
| 文本型 PDF | `pdf_parser.py` | PyMuPDF 提取文字和结构 |
| 扫描件 PDF/图片 | `scanned/` 子模块 | 七阶段处理管线 |

**扫描件处理管线**（7 阶段）：
```
1. 图像预处理（preprocessor.py）
   → 去噪、增强、纠偏
2. OCR 文字识别（ocr_engine.py）
   → PaddleOCR PPStructureV3
3. 版面结构识别（structure.py）
   → 段落、标题、表格区域分割
4. 间距计算（spacing.py）
   → 行距、段距、缩进
5. 字体检测（font_detector.py）
   → 字号、加粗、倾斜
6. 归一化处理
   → 统一数据表示
7. LLM 补全与纠错（llm_client.py）
   → OCR 错误修正、缺失内容补全
```

**OCR 引擎抽象**：
- `ocr/base.py` — 抽象基类
- `ocr/factory.py` — 工厂模式（支持切换 OCR 实现）
- `ocr/ppstructure.py` — PaddleOCR 适配器

**解析注册表**：`parsers/registry.py` 根据文件类型和内容特征选择合适的解析器。

### 5.2 格式审核 (format_audit)

**插件**: `plugins/format_audit/`

基于路径式要素比对模型，将文档结构与 GB/T 9704-2012 标准模板进行逐要素比对：

1. **文档类型检测** (`detect_document_type`)：识别公文文种（通知、报告、请示等 15 种）
2. **逐要素比对** (`audit_format`)：
   - 字体（字号、加粗、倾斜）
   - 位置（页边距、对齐方式、缩进）
   - 间距（行距、段前段后）
   - 结构（标题层级、正文、附件、落款）

**输出**：带位置标注的格式违规清单。

### 5.3 内容审核 (content_audit)

**插件**: `plugins/content_audit/`

LLM 驱动的内容审查：

```
阶段 1：领域分类
  → 识别文档所属政务领域（20 个预定义领域：财政、教育、医疗、交通等）
  → 由 audit_content 内部调用 ContentChecker.classify_text 完成

阶段 2：规则驱动审核（audit_content）
  → 加载领域特定规则
  → 长文档 asyncio.gather 并行分块处理
  → LLM 逐条规则校验
  → 输出违规项清单
```

**规则系统**：规则库由 `content_compliance/` 模块管理，支持 CRUD 和批量导入。

### 5.4 文本纠错 (doccorrector)

**文件**: `src/doccorrector/corrector.py`

六阶段纠错流水线：

```
阶段 1：用户词典加载
  → 加载政务领域专用词汇表
  → 防止误纠正官方用词

阶段 2：专用纠错模型
  → ChineseErrorCorrector3-4B 模型
  → 处理拼写、多字、漏字、倒序等错误

阶段 3：规则检测（由 correct_text 内部统一调用）
  → 重复字符检测
  → 全角/半角混用检测
  → 标点混用检测
  → 截断姓名检测

阶段 4：冲突消解
  → 纠错模型 vs 规则的冲突判断
  → 双层置信度投票

阶段 5：LLM 最终确认
  → 低置信度结果送 LLM 判定

阶段 6：上下文一致性检查
  → 全局一致性校验
```

### 5.5 行文风格审查 (style_audit)

**插件**: `plugins/style_audit/`

可插拔的 Protocol + Registry 架构：

```
StyleChecker(Protocol)
  ├── 基于文种 × 子类型维度的检查器
  ├── Registry 动态注册/发现
  └── 支持自定义检查规则
```

检查维度包括：
- 行文规范性（公文用语、套话使用）
- 段落结构（总分总、层次递进）
- 语气一致性（正式程度、人称统一）
- 篇幅控制（各部分长度比例）

### 5.6 文档查重 (plagiarism)

**文件**: `src/plagiarism/core.py`

段落级相似度检测（由 detect_plagiarism 统一封装）：

```
1. 文档向量化
   → TF-IDF / 文本嵌入
2. 段落级相似度计算
   → 余弦相似度 / SequenceMatcher
3. 动态阈值计算
   → Tukey IQR 方法（Q3 + 1.5 × IQR）
4. 双门控机制（detect_plagiarism）
   → 全局相似度阈值 + 局部段落匹配度
   → 排除长度极短段落的假阳性
5. 输出可疑段落对及相似度分数
```

### 5.7 批注导出 (docannot)

**文件**: `src/docannot/`

审核结果汇总为带批注的可导出文档：

**实现原理**：
- DOCX：通过 `python-docx` 插入 comments（批注）
- PDF：通过 PyMuPDF 添加标注（highlight + annotation）

**核心文件**：
- `_annotate.py` — 主标注逻辑
- `_traverse.py` — 文档结构遍历（获取段落路径）
- `_rule.py` — 批注规则模型
- `_patch.py` — python-docx 补丁（支持复杂批注场景）

### 5.8 文档构建 (docbuilder)

**文件**: `src/docbuilder/`

从数据模型生成符合格式规范的 DOCX 文档：

- `DocumentBuilder` — 文档构建器（段落、标题、表格等元素组合）
- `ParagraphBuilder` — 段落构建器
- `converter.py` — 格式转换
- `styles.py` — 预定义样式（GB/T 9704 标准样式）

### 5.9 内容合规引擎 (content_compliance)

**文件**: `src/content_compliance/`

可扩展的规则引擎：

```python
class RuleEngine:
    """基于正则的规则检查引擎。"""
    
class CheckerRegistry:
    """可插拔的检查器注册表。
    支持内置检查器和插件提供的 ProxyChecker。"""
    
class ComplianceResult:
    is_valid: bool
    violations: list[Violation]
```

---

## 6. 数据层

### 6.1 数据库层 (dbop)

**文件**: `src/dbop/`

基于 SQLAlchemy 2.x 异步 ORM 的数据访问层：

```python
# 连接管理
class DBManager:
    async def get_session() -> AsyncSession: ...
    async def close(): ...

# 数据表模型 (tables/)
Document    # 文档信息（文件名、类型、大小、上传时间、处理状态）
Element     # 文档元素（段落、标题、表格等）
Page        # 页面信息
Paragraph   # 段落详情
Rule        # 内容审核规则
Library     # 文档资源库
FormatTemplate  # 格式模板
Resource    # 资源文件
AuditResult # 审核结果记录
```

**关键文件**：
- `db_manager.py` — 连接管理和会话工厂
- `load_doc.py` — 文档读取操作
- `save_doc.py` — 文档写入操作
- `tables/` — SQLAlchemy 表模型定义

### 6.2 Elasticsearch 层 (es)

**文件**: `src/es/client.py`

全文检索和语义搜索的基础设施：

- **文档索引**：文档内容、章节、段落的全文索引
- **RAG 检索**：用于审核助手对话中的知识增强
- **多字段搜索**：支持标题、内容、标签等维度
- **高亮**：搜索结果中的关键词高亮

### 6.3 对象存储层 (storage)

**文件**: `src/storage/`

基于 MinIO（S3 兼容）的对象存储：

```python
class StorageBase(Protocol):
    async def upload(key, data, content_type) -> str: ...
    async def download(key) -> bytes: ...
    async def delete(key): ...
    async def get_url(key) -> str: ...

class MinIOBackend(StorageBase):
    """MinIO 实现。"""

class CacheStorage(StorageBase):
    """带缓存层的存储装饰器。"""
```

**存储内容**：
- 上传的原始文件（PDF、DOCX、图片）
- 解析后的中间结果
- 审核结果（JSON）
- 导出的批注文档

---

## 7. API 层

### 7.1 应用工厂

**文件**: `src/agent/api/app.py`

```python
def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。
    
    启动流程：
    1. 加载 Settings（.env or 环境变量）
    2. 配置结构化日志
    3. 初始化内容合规检查器
    4. 创建 PluginSystem 并启动插件
    5. 配置 CORS 中间件
    6. 配置可观测性中间件
    7. 挂载路由
    8. 暴露 /metrics 端点
    """
```

### 7.2 路由结构

**文件**: `src/agent/api/routes/__init__.py`

| 路由前缀 | 文件 | 功能 |
|----------|------|------|
| `/api/sessions` | `sessions.py` | 创建/查询/删除会话，SSE 流式审核 |
| `/api/files` | `files.py` | 文件上传 |
| `/api/control` | `control.py` | 暂停/恢复/停止会话 |

### 7.3 SSE 流式响应

**文件**: `src/agent/api/sse_adapter.py`

```python
class SSEAdapter:
    """将 Agent 循环事件转换为 SSE 格式。
    
    事件类型：
    - event: step     → 阶段变更（think/act/observe）
    - event: token    → LLM 推理 token（思维链）
    - event: delta    → LLM 内容 token（最终回复）
    - event: tool     → 工具执行结果
    - event: error    → 错误信息
    - event: done     → 流结束
    """
```

### 7.4 会话管理

**文件**: `src/agent/api/session_store.py`

```python
class SessionStore:
    """文件后端会话持久化。"""
    
    async def create_session(session_id, mode) -> SessionRecord: ...
    async def get_session(session_id) -> SessionRecord | None: ...
    async def add_step(session_id, step) -> None: ...
    async def delete_session(session_id) -> None: ...
```

**SessionRecord 模型** (`models.py`)：
```python
class SessionRecord:
    id: str
    mode: str           # "audit" | "chat"
    status: str         # "active" | "paused" | "completed" | "error"
    steps: list[StepRecord]
    created_at: datetime
    updated_at: datetime

class StepRecord:
    phase: str          # "think" | "act" | "observe"
    detail: str         # 阶段详情
    timestamp: datetime

class ThoughtRecord:
    """LLM 思考过程记录（推理内容分离）。"""

class ToolInfo:
    """工具调用信息。"""
```

### 7.5 服务层

| 服务 | 文件 | 职责 |
|------|------|------|
| `AgentService` | `services/agent_service.py` | Agent 构造和配置（构建审核 Agent、对话 Agent） |
| `FileService` | `services/file_service.py` | 文件上传、存储、格式验证 |
| `SessionService` | `services/session_service.py` | 会话 CRUD 操作 |
| `StreamService` | `services/stream_service.py` | SSE 流生成、状态序列化、事件编排 |

### 7.6 控制端点

**文件**: `src/agent/api/routes/control.py`

```python
POST /api/control/{session_id}/pause   # 暂停 Agent 执行
POST /api/control/{session_id}/resume  # 恢复 Agent 执行
POST /api/control/{session_id}/stop    # 停止 Agent 执行（发送 request.cancel 到所有插件）
```

---

## 8. 可观测性

### 8.1 OpenTelemetry 追踪

**文件**: `src/agent/telemetry/tracer.py`

```python
class AgentTracer:
    def agent_span(agent_name, session_id, task) -> Span:
        """Agent 级别的追踪 span。"""
        
    def llm_span(model, temperature) -> Span:
        """LLM 调用级别的追踪 span。"""
        
    def tool_span(tool_name, parameters) -> Span:
        """工具调用级别的追踪 span。"""
```

**追踪结构**：
```
agent_span (session_id, task)
├── llm_span (turn 1)
│   ├── prompt event
│   └── completion event (token usage)
├── tool_span: parse_document
├── tool_span: format_audit (SkillTool)
│   └── subagent_span (通用 Agent 内部循环)
│       ├── llm_span (子代理 LLM 调用)
│       └── tool_span: audit_format
│           └── plugin_span (跨进程 JSON-RPC)
├── llm_span (turn 2)
...
```

**上下文传播**：OpenTelemetry trace context 通过 JSON-RPC params 在插件间传播。

### 8.2 Prometheus 指标

**文件**: `src/agent/telemetry/metrics.py`

| 指标 | 类型 | 描述 |
|------|------|------|
| `agent_requests_total` | Counter | Agent 请求总数（按 name + status） |
| `llm_calls_total` | Counter | LLM 调用总数（按 model + agent_name） |
| `llm_tokens_total` | Counter | Token 消耗总量（按 model + type） |
| `tool_executions_total` | Counter | 工具执行总数（按 tool_name + status） |
| `tool_latency_seconds` | Histogram | 工具执行延迟分布 |

### 8.3 审计日志

**文件**: `src/agent/core/audit_logger.py`

```python
class AuditLogger:
    """结构化审计日志。
    
    记录每个 Agent 运行的完整历史：
    - 每轮 LLM 请求/响应
    - 工具调用和结果
    - Token 用量
    - 最终状态和终止原因
    """
    
    def log_turn(turn_index, request, response, tool_records): ...
    def finalize(final_status, termination_reason): ...
```

### 8.4 日志配置

**文件**: `src/agent/core/logging_config.py`

结构化日志配置，支持 JSON 格式输出，便于日志聚合和分析。

### 8.5 可观测性中间件

**文件**: `src/agent/api/middleware/observability.py`

FastAPI 中间件，自动记录 HTTP 请求的追踪信息和指标。

### 8.6 可观测性基础设施

| 组件 | 配置 | 用途 |
|------|------|------|
| OpenTelemetry Collector | `otel-collector-config.yaml` | 收集、处理、导出遥测数据 |
| Prometheus | `prometheus.yml` | 指标采集和存储 |
| Grafana | docker-compose 中配置 | 可视化仪表板 |
| Langfuse | docker-compose 中配置 | LLM 可观测性平台（需要 ClickHouse） |

---

## 9. 部署架构

### 9.1 Docker Compose 拓扑

```
┌─────────────────────────────────────────────────────────┐
│                     Docker Network                       │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │   app    │  │  mysql   │  │  minio   │              │
│  │ (8001)   │  │ (3306)   │  │ (9000)   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │   es     │  │ppocr     │  │ otel-col │              │
│  │ (9200)   │  │ (8010)   │  │ (4317)   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ langfuse │  │prometheus│  │ grafana  │              │
│  │ (3000)   │  │ (9090)   │  │ (3000)   │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│                                                          │
│  app 依赖: mysql, minio, es, ppocr, otel-col            │
│  langfuse 依赖: clickhouse                               │
└─────────────────────────────────────────────────────────┘
```

### 9.2 外部服务

**数据库** (MySQL 8.x)：
- 存储文档、规则、模板、审核结果
- Alembic 管理 schema 迁移

**搜索引擎** (Elasticsearch 8.x)：
- 全文索引（文档内容、段落）
- RAG 检索增强生成
- 文档查重库搜索

**对象存储** (MinIO)：
- 上传文件和解析结果的持久化存储
- S3 兼容 API

**OCR 服务** (PaddleOCR PPStructureV3)：
- 独立 HTTP 服务
- 支持文字识别、版面分析、表格识别

**LLM 服务**：
- 通义千问 Qwen3.5-27B（通用推理）
- ChineseErrorCorrector3-4B（专项纠错）
- OpenAI 兼容 API 接口

---

## 10. 核心数据流与运行流程

### 10.1 文档审核完整流程

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         文档审核端到端流程                                │
└─────────────────────────────────────────────────────────────────────────┘

用户上传文件
    │
    ▼
┌──────────────────┐
│  File Upload     │  POST /api/files → 保存到 MinIO
└──────────────────┘
    │
    ▼
┌──────────────────┐
│  Session Create  │  POST /api/sessions → 创建会话记录
└──────────────────┘
    │
    ▼
┌──────────────────┐
│  SSE Stream      │  GET /api/sessions/{id}/stream
│  (Agent 启动)     │
└──────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    OrchestratorAgent.run()                            │
│                                                                       │
│  Turn 1: LLM → 调用 parse_document 工具                              │
│    │                                                                  │
│    ├── ProxyTool.execute("parse_document")                            │
│    │   └── JSON-RPC → parse plugin 子进程                             │
│    │       └── docparse 解析引擎（PDF/DOCX/扫描件）                    │
│    │           └── 返回 ParsedDocument 结构                           │
│    │                                                                  │
│    └── $ref:parse_document:1  ← CacheStore 持久化                     │
│        └── ArtifactStore.register(parsed_document)                     │
│                                                                       │
│  Turn 2: LLM → 调用 format_audit(task="...")                          │
│    │                                                                  │
│    ├── SkillTool.execute("format_audit", task)                        │
│    │   ├── 取 SkillConfig + AgentRuntime.spawn()                       │
│    │   └── AgentRuntime.delegate() 运行通用 Agent（SubAgent）          │
│    │       └── SubAgent 内部循环依次调用工具：                          │
│    │           ├── detect_document_type                                │
│    │           ├── audit_format                                        │
│    │           └── 汇总产出 FormatAuditResult                          │
│    │                                                                  │
│    └── ExecutionResult(metadata: is_subagent_result)                  │
│        └── 流式事件: token, think, tool_result (scope: subagent)      │
│                                                                       │
│  Turn 3-5: 顺序调用其它审核 Skill 工具                                  │
│    ├── content_audit(task=...)    → 内容审核                          │
│    ├── text_correction(task=...)  → 文本纠错                          │
│    └── style_audit(task=...)      → 行文风格审查                      │
│                                                                       │
│  Turn 6: LLM → 调用 annotate_document                                  │
│    └── ProxyTool.execute("annotate_document")                          │
│        └── docannot 模块 → 生成带批注的 DOCX/PDF                       │
│                                                                       │
│  Turn 7: LLM 输出最终摘要文本（无工具调用，循环终止）                     │
└──────────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────┐
│  SSE: done       │  流式响应结束
└──────────────────┘
    │
    ▼
┌──────────────────┐
│  结果查询/下载     │  GET /api/results/{id} → 审核结果 + 批注文件
└──────────────────┘
```

### 10.2 审核助手对话流程

```
用户发送消息
    │
    ▼
┌────────────────────────────────────────────┐
│         OrchestratorAgent.run()            │
│                                            │
│  1. 接收用户任务                            │
│  2. 按需调用 search_documents 检索本地文档  │
│  3. 上下文注入                              │
│     └── 搜索结果拼接到对话上下文             │
│  4. LLM 流式生成                            │
│     └── AsyncOpenAI streaming              │
│         ├── SSE: delta (token 级别)         │
│         └── SSE: resources (引用来源)       │
│                                            │
│  （原 AssistantAgent 的 RAG/网关搜索已废弃； │
│   本地检索由 OrchestratorAgent 统一处理）    │
└────────────────────────────────────────────┘
```

### 10.3 插件子代理执行流程

```
Host Process                                  Plugin Subprocess
────────────                                  ─────────────────

AgentRuntime.delegate(handle)
  │
  ├── 构造 Pydantic input（解析 $ref）
  ├── 获取 ScopedArtifactStore
  │
  └── ProxyAgent.run(task, tools, ...)
      │
      └── JSONRPCClient.stream("agent.run")
          │
          │  ──── Request ────▶
          │                     PluginRuntime.run_agent()
          │                       │
          │                       ├── 构建 system prompt
          │                       ├── 注册工具 schemas
          │                       │
          │                       │  ┌── Agent Loop ──┐
          │                       │  │  THINK: LLM    │
          │  ◀── chunk(token) ─── │  │    ↓           │
          │  ◀── chunk(think) ──  │  │  TOOL: execute │
          │                       │  │    ↓           │
          │                       │  │  OUTPUT: chunk │
          │                       │  └────────────────┘
          │                       │
          │  ◀── chunk(end) ───── │  返回 AgentResult
          │
          └── 返回 AgentResult
              └── 注册产出 Artifact
```

### 10.4 插件生命周期

```
启动阶段:
  PluginScanner.scan() → plugin.yaml 验证
  ProcessManager._start_one()
  ├── uv run entry.py (子进程启动)
  ├── 子进程发送 plugin.register 通知
  ├── ExtensionRegistry.on_register()
  │   ├── ProxyTool 注册到 ToolRegistry
  │   ├── ProxyChecker 注册到 CheckerRegistry
  │   ├── ProxyAgent 存储到 AgentRegistry
  │   └── 系统提示词收集
  └── 状态 → ACTIVE

运行阶段:
  每 30s: plugin.health 健康检查
  按需: tool.execute / agent.run / checker.check RPC 调用
  Stderr 监控: 崩溃检测 → _on_crash()

终止阶段:
  PluginSystem.shutdown()
  ├── 发送 plugin.shutdown 通知
  ├── SIGTERM → 等待 5s → SIGKILL
  └── 状态 → STOPPED

崩溃恢复:
  CRASHED → RESTARTING (指数退避: 1s, 2s, 4s)
  ├── 成功 → 重新注册 → ACTIVE
  ├── 再次崩溃 → 继续重试（最多 3 次）
  └── 启动 5s 内崩溃 → FATAL（不重启）
```

---

## 附录

### A. 目录结构完整清单

```
docaudit-agent/
├── main.py                    # 应用入口
├── pyproject.toml             # 项目配置与依赖
├── Dockerfile                 # 应用镜像
├── Dockerfile.env             # 环境镜像
├── docker-compose.yml         # Docker 编排
├── .env / .env.example        # 环境变量配置
├── alembic.ini                # 数据库迁移配置
├── CLAUDE.md                  # 项目规则
├── README.md                  # 项目说明
│
├── src/
│   ├── config.py              # Pydantic Settings 配置
│   ├── agent/                 # AI Agent 核心系统
│   │   ├── agents/            # Agent 实现
│   │   │   ├── base.py        # Agent 基类
│   │   │   ├── orch.py        # 编排 Agent
│   │   │   ├── echo.py        # 测试 Agent
│   │   │   ├── input_models.py# 子代理结构化输入模型
│   │   │   └── subagent/      # 子代理系统
│   │   │       ├── config.py      # SubAgentConfig
│   │   │       └── events.py      # 流事件类型
│   │   ├── runtime/           # 子代理运行时
│   │   │   ├── runtime.py         # AgentRuntime
│   │   │   ├── handle.py          # AgentHandle
│   │   │   ├── budget.py          # AgentRuntimeBudget
│   │   │   ├── result.py          # ExecutionResult
│   │   │   ├── summarizer.py      # ResultSummarizer
│   │   │   └── store.py           # ResultStore
│   │   ├── api/               # FastAPI 层
│   │   │   ├── app.py             # 应用工厂
│   │   │   ├── models.py          # API 数据模型
│   │   │   ├── file_store.py      # 文件存储
│   │   │   ├── session_store.py   # 会话存储
│   │   │   ├── sse_adapter.py     # SSE 适配器
│   │   │   ├── routes/            # 路由
│   │   │   │   ├── sessions.py    # 会话 + SSE
│   │   │   │   ├── files.py       # 文件上传
│   │   │   │   └── control.py     # 控制端点
│   │   │   ├── services/          # 服务层
│   │   │   │   ├── agent_service.py
│   │   │   │   ├── file_service.py
│   │   │   │   ├── session_service.py
│   │   │   │   └── stream_service.py
│   │   │   └── middleware/        # 中间件
│   │   │       └── observability.py
│   │   ├── artifacts/         # 工件系统
│   │   │   ├── models.py          # Artifact, ArtifactSchema
│   │   │   ├── store.py           # ArtifactStore
│   │   │   ├── binder.py          # ContractBinder
│   │   │   ├── projectors.py      # ProjectorRegistry
│   │   │   ├── resolver.py        # 投影解析
│   │   │   └── executor.py        # 投影执行
│   │   ├── core/              # 核心循环
│   │   │   ├── state.py           # AgentState (不可变)
│   │   │   ├── model.py           # ModelClient
│   │   │   ├── loop.py            # agent_loop()
│   │   │   ├── loop_phases.py     # think/execute 阶段
│   │   │   ├── loop_guards.py     # 护栏检测
│   │   │   ├── loop_hints.py      # 提示注入
│   │   │   ├── loop_utils.py      # 工具函数
│   │   │   ├── loop_audit.py      # 审计日志写入
│   │   │   ├── context_manager.py # 三层上下文控制
│   │   │   ├── cache_store.py     # $ref 缓存
│   │   │   ├── audit_logger.py    # 审计日志
│   │   │   ├── logging_config.py  # 日志配置
│   │   │   ├── schema_utils.py    # Schema 工具
│   │   │   └── loop_streaming.py  # 流式处理
│   │   ├── hooks/             # 钩子系统
│   │   │   ├── __init__.py        # 公共 API 导出
│   │   │   └── chain.py           # HookChain
│   │   ├── memory/            # 记忆系统
│   │   │   └── store.py           # MemoryStore
│   │   ├── permissions/       # 权限系统
│   │   │   └── gate.py            # PermissionGate
│   │   ├── prompts/           # Prompt 系统
│   │   │   └── pipeline.py        # PromptPipeline
│   │   ├── tools/             # 工具系统
│   │   │   ├── protocol.py        # ToolProtocol
│   │   │   ├── registry.py        # ToolRegistry
│   │   │   ├── summary.py         # 工具结果摘要
│   │   │   └── builtin/           # 内置工具
│   │   ├── telemetry/         # 可观测性
│   │   │   ├── tracer.py          # AgentTracer
│   │   │   ├── metrics.py         # Prometheus 指标
│   │   │   ├── context.py         # 追踪上下文
│   │   │   └── decorators.py      # 装饰器
│   │   └── testing/           # 测试工具
│   │
│   ├── plugin/                # 插件系统
│   │   ├── __init__.py            # PluginSystem
│   │   ├── manifest.py            # PluginManifest
│   │   ├── scanner.py             # PluginScanner
│   │   ├── protocol.py            # JSON-RPC 消息类型
│   │   ├── client.py              # JSONRPCClient
│   │   ├── manager.py             # ProcessManager
│   │   ├── registry.py            # ExtensionRegistry
│   │   ├── proxies.py             # ProxyTool/Checker/Agent
│   │   └── sdk/                   # 插件 SDK
│   │       ├── protocol.py        # 协议常量
│   │       └── runtime.py         # PluginRuntime
│   │
│   ├── content_compliance/    # 内容合规引擎
│   ├── dbop/                  # 数据库操作层
│   ├── plagiarism/            # 文档查重
│   ├── docannot/              # 批注导出
│   ├── docbuilder/            # 文档构建
│   ├── doccorrector/          # 文本纠错
│   ├── docmodels/             # 数据模型
│   ├── docparse/              # 文档解析
│   ├── es/                    # Elasticsearch
│   ├── storage/               # MinIO 存储
│   └── validator/             # 格式校验
│
├── plugins/                   # 插件目录 (9 个插件)
│   ├── annotate/
│   ├── content_audit/
│   ├── format_audit/
│   ├── parse/
│   ├── plagiarism/
│   ├── search/
│   ├── style_audit/
│   ├── template/
│   └── text_correction/
│
├── tests/                     # 测试
├── docs/                      # 文档
│   └── agent/
│       ├── architecture.md
│       └── modules/
│           └── hook-system-guide.md
├── scripts/                   # 工具脚本
├── data/                      # 运行时数据
├── alembic/                   # 数据库迁移
└── uploads/                   # 文件上传目录
```

### B. 关键设计决策

| 决策 | 理由 |
|------|------|
| AgentState 不可变（frozen Pydantic） | 防止并发场景下的状态腐败；每次变更创建新实例，语义清晰 |
| 插件子进程隔离 (JSON-RPC over stdio) | 进程间崩溃隔离；语言无关性；支持热注册/热恢复 |
| 三层上下文预算控制 | 在 LLM 上下文窗口限制下，最大化信息利用率 |
| $ref 数据引用传递 | 避免大型数据在上下文中的重复序列化；节省 token 消耗 |
| 流式 JSON-RPC（agent.run） | 支持长运行子代理操作的实时事件流和心跳检测 |
| 插件自动崩溃恢复（指数退避） | 提高系统鲁棒性；避免无限重启（熔断器机制） |
| 合约驱动的 Artifact 绑定 | 减少 LLM 需手动传递的参数；类型安全的数据流 |
| PromptPipeline 六段式结构 | 结构化 prompt 构建；支持灵活扩展和调试 |
| 护栏机制（Explore Loop / Business Progress） | 防止 LLM 陷入无意义循环或在探索阶段浪费资源 |
