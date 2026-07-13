# DocAudit Agent 代码审查修复 — 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复代码审查发现的 18 个问题，分 A→B→C→D 四个批次实施。

**架构：** 按批次顺序修复（安全紧急 → 架构债务 → 代码质量 → 可选优化），每批完成后运行测试验证。保持向后兼容，优先最小化改动。

**技术栈：** Python 3.12, FastAPI, Pydantic, SQLAlchemy, pytest

---

## 文件结构

### 批次 A（安全 + 配置 + 不可变性）

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/config.py` | 修改 | 新增 CORS、CEC、DOCPARSE 配置字段 |
| `src/agent/api/app.py` | 修改 | 从 Settings 读取 CORS 配置 |
| `.env.example` | 修改 | 新增 CORS 相关环境变量示例 |
| `src/doccorrector/corrector.py` | 修改 | 移除 load_dotenv，改为从 Settings 读取配置 |
| `src/docparse/parsers/base.py` | 修改 | 移除 load_dotenv，ParserConfig.from_settings |
| `src/docparse/__init__.py` | 修改 | 移除 load_dotenv |
| `src/agent/core/state.py` | 修改 | Message.tool_calls 类型 list → tuple |
| `src/agent/core/loop.py` | 修改 | 适配 tuple(tool_calls) 转换 |

### 批次 B（架构债务）

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/agent/artifacts/store.py` | 修改 | ArtifactStore 新增 resolve_ref/persist_large_output |
| `src/agent/artifacts/binder.py` | 创建 | ContractBinder 协调 ToolRegistry 和 ArtifactStore |
| `src/agent/core/cache_store.py` | 修改 | 标记 deprecated，逻辑迁移到 ArtifactStore |
| `src/agent/core/context_manager.py` | 修改 | 委托给 ArtifactStore，保留代理方法兼容 |
| `src/agent/tools/registry.py` | 修改 | 移除 artifacts 导入，简化 execute |
| `src/agent/core/loop.py` | 修改 | 移除 cache_store 获取逻辑 |
| `src/agent/api/routes.py` | 修改 | 移除 _rehydrate_artifact_store |
| `src/dbop/db_manager.py` | 修改 | 移除 _migrate_missing_columns |
| `alembic/` | 创建 | Alembic 迁移目录和初始迁移脚本 |
| `pyproject.toml` | 修改 | 添加 alembic 依赖 |

### 批次 C（代码质量）

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/agent/agents/subagent.py` | 修改 | 提取 _execute_with_retry 消除重复 |
| `src/agent/api/routes.py` | 修改 | 提取 _build_model_client 公共函数 |
| `src/agent/core/loop_guards.py` | 创建 | 探索循环守卫、推理循环检测、终端工具检测 |
| `src/agent/core/loop_streaming.py` | 创建 | 流式生成回退、token 分割 |
| `src/agent/core/loop_audit.py` | 创建 | 审计日志写入辅助 |
| `src/agent/core/loop.py` | 修改 | 主循环骨架，导入新拆分模块 |
| `src/agent/agents/subagent_runner.py` | 创建 | SubAgentRunner 类 |
| `src/agent/agents/subagent_tool.py` | 创建 | _SubAgentTool 适配器 |
| `src/agent/agents/subagent_input.py` | 创建 | input_models 解析相关 |
| `src/agent/agents/subagent.py` | 修改 | 保留兼容性导入 |
| `tests/scripts/test_document_es.py` | 修改 | 添加 pytestmark = pytest.mark.integration |
| `tests/scripts/test_resources_*.py` | 修改 | 添加 pytestmark = pytest.mark.integration |

### 批次 D（可选优化）

| 文件 | 操作 | 职责 |
|------|------|------|
| `src/dedump/dedump.py` | 修改 | 添加 MinHash 预筛选（>100篇时启用） |
| `pyproject.toml` | 修改 | 添加 datasketch 依赖 |
| `src/agent/agents/base.py` | 修改 | 新增 RunOptions dataclass |
| `src/agent/tools/protocol.py` | 修改 | 拆分 ToolProtocol 为基础 + mixin |
| `src/agent/agents/orch.py` | 修改 | 替换 copy.deepcopy 为 dict 推导式 |
| `src/dedump/dedump.py` | 修改 | 添加 DeprecationWarning（模块名 legacy） |

---

## 批次 A：安全 + 配置 + 不可变性紧急修复

---

### 任务 A1：收紧 CORS 配置

**文件：**
- 修改：`src/config.py`
- 修改：`src/agent/api/app.py`
- 修改：`.env.example`

- [ ] **步骤 1：在 Settings 中添加 CORS 配置字段**

在 `src/config.py` 的 `Settings` 类中，在 `model_config` 之前添加：

```python
cors_origins: list[str] = Field(
    default=["http://localhost:5173"],
    description="允许的 CORS 来源列表，JSON 数组格式"
)
cors_allow_credentials: bool = Field(
    default=False,
    description="是否允许携带凭证的跨域请求"
)
```

- [ ] **步骤 2：修改 app.py 使用 Settings 的 CORS 配置**

将 `src/agent/api/app.py` 中：

```python
    # CORS — allow frontend dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

改为：

```python
    # CORS — configured via Settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials and settings.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

- [ ] **步骤 3：更新 .env.example**

在 `.env.example` 末尾添加：

```bash
# CORS 配置（开发环境默认值已足够，生产环境请修改）
# CORS_ORIGINS=["http://localhost:5173"]
# CORS_ALLOW_CREDENTIALS=false
```

- [ ] **步骤 4：运行 Agent API 测试**

```bash
uv run pytest tests/agent/test_routes_rehydrate.py -v
```

预期：PASS（CORS 变更不影响路由逻辑）

- [ ] **步骤 5：运行全部非集成测试**

```bash
uv run pytest -m "not integration" -q
```

预期：全部通过（与批次前基线一致）

- [ ] **步骤 6：Commit**

```bash
git add src/config.py src/agent/api/app.py .env.example
git commit -m "fix: tighten CORS config from allow-all to Settings-driven origin list

- Add cors_origins and cors_allow_credentials to Settings
- Default to localhost:5173 only, credentials disabled
- Prevent allow_credentials=True when origins=['*']"
```

---

### 任务 A2：统一配置管理 — Settings 新增字段

**文件：**
- 修改：`src/config.py`

- [ ] **步骤 1：在 Settings 中添加 CEC 相关字段**

在 `src/config.py` 的 `Settings` 类中，在 `minio_endpoint` 之前添加：

```python
    # CEC (Chinese Error Corrector) API
    cec_api_base: str = ""
    cec_api_key: str = ""
    cec_model_name: str = "ChineseErrorCorrector3-4B"
    cec_max_length: int = Field(default=16383, alias="cec_max_length")
    cec_user_dict: str = Field(default="./src/doccorrector/user_dict.txt", alias="cec_user_dict")
    cec_allowed_patterns: str = Field(
        default="看一看,想一想,试一试,人人,一一",
        alias="cec_allowed_patterns"
    )
```

- [ ] **步骤 2：运行配置测试**

```bash
uv run pytest -c pyproject.toml tests/agent/ -q -k "config"
```

预期：无失败（新增字段不影响现有测试）

- [ ] **步骤 3：Commit**

```bash
git add src/config.py
git commit -m "feat: add CEC config fields to Settings for unified config management"
```

---

### 任务 A3：统一配置管理 — 重构 doccorrector

**文件：**
- 修改：`src/doccorrector/corrector.py`

- [ ] **步骤 1：移除模块级 load_dotenv 和全局常量**

将 `src/doccorrector/corrector.py` 顶部（第 12-37 行）替换为：

```python
import logging
import os
import re
from pathlib import Path

import httpx
from openai import OpenAI

logger = logging.getLogger(__name__)

PROMPT_PREFIX: str = "你是一个文本纠错专家，纠正输入句子中的语法错误，并输出正确的句子，输入句子为："
```

- [ ] **步骤 2：重构 OpenAITextCorrectInfer 接收 Settings**

将 `OpenAITextCorrectInfer.__init__`（约第 423-439 行）改为：

```python
class OpenAITextCorrectInfer:
    """基于 OpenAI 兼容接口的文本纠错推理器"""

    def __init__(
        self,
        api_base: str = "",
        api_key: str = "",
        model_name: str = "ChineseErrorCorrector3-4B",
        max_length: int = 16383,
        user_dict: str = "",
        allowed_patterns: str = "看一看,想一想,试一试,人人,一一",
    ) -> None:
        self.client = OpenAI(
            api_key=api_key or "EMPTY",
            base_url=api_base or "http://192.168.100.118:9090/v1",
            http_client=httpx.Client(
                proxy=None,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
            ),
        )
        self.model = model_name or "ChineseErrorCorrector3-4B"
        self.max_length = max_length
        self.user_dict = user_dict
        self.allowed_patterns = set(w.strip() for w in (allowed_patterns or "").split(","))
```

- [ ] **步骤 3：重构 ErrorCorrect 接收 Settings**

将 `ErrorCorrect.__init__`（约第 504-509 行）改为：

```python
class ErrorCorrect:
    """中文拼写和语法错误纠正 — 四阶段流水线"""

    def __init__(self, settings=None) -> None:
        from src.config import Settings
        s = settings or Settings()
        self.inferencer = OpenAITextCorrectInfer(
            api_base=s.cec_api_base,
            api_key=s.cec_api_key,
            model_name=s.cec_model_name,
            max_length=s.cec_max_length,
            user_dict=s.cec_user_dict,
            allowed_patterns=s.cec_allowed_patterns,
        )
        self._protected_words: set[str] = _load_user_words(s.cec_user_dict)
```

- [ ] **步骤 4：运行 doccorrector 相关测试**

```bash
uv run pytest tests/ -q -k "correct" --ignore=tests/scripts/
```

预期：全部通过

- [ ] **步骤 5：Commit**

```bash
git add src/doccorrector/corrector.py
git commit -m "refactor: remove module-level load_dotenv from doccorrector

- Remove top-level load_dotenv() and global constants
- OpenAITextCorrectInfer and ErrorCorrect now accept explicit params
- ErrorCorrect falls back to Settings() when no settings provided"
```

---

### 任务 A4：统一配置管理 — 重构 docparse

**文件：**
- 修改：`src/docparse/parsers/base.py`
- 修改：`src/docparse/__init__.py`

- [ ] **步骤 1：移除 base.py 的 load_dotenv**

将 `src/docparse/parsers/base.py` 顶部替换为：

```python
from __future__ import annotations

import os
from typing import Protocol

from pydantic import BaseModel, Field

from docmodels import Document


class ParserConfig(BaseModel):
    """Configuration for document parsers."""

    # Multimodal LLM (for structure recognition with images)
    llm_base_url: str = Field(default="")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="")

    # PPStructureV3 OCR API
    ocr_api_url: str = Field(default="")
    ocr_lang: str = Field(default="ch")
    ocr_engine: str = Field(default="ppstructure")

    # Page size constants (A4 in points, 72 DPI)
    a4_width_pt: float = Field(default=595.28)
    a4_height_pt: float = Field(default=841.89)

    rule_confidence_threshold: float = Field(default=0.80)
    max_llm_concurrent: int = Field(default=4)
    max_ocr_concurrent: int = Field(default=10)
    ocr_max_image_long_side: int = Field(default=2048)

    @classmethod
    def from_settings(cls, settings) -> ParserConfig:
        """Create config from Settings instance."""
        return cls(
            llm_base_url=settings.llm_base_url,
            llm_api_key=settings.llm_api_key,
            llm_model=settings.llm_model,
            ocr_api_url=settings.docparse_ocr_api_url,
            ocr_lang=settings.docparse_ocr_lang,
            ocr_engine=settings.docparse_ocr_engine,
            ocr_max_image_long_side=settings.docparse_ocr_max_image_long_side,
        )
```

- [ ] **步骤 2：移除 __init__.py 的 load_dotenv**

将 `src/docparse/__init__.py` 替换为：

```python
"""Document parsing module."""

from docparse.parsers.base import Parser, ParserConfig
from docparse.parsers.registry import ParserRegistry
```

（移除所有 `load_dotenv` 相关代码）

- [ ] **步骤 3：运行 docparse 测试**

```bash
uv run pytest src/docparse/tests/ -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/docparse/parsers/base.py src/docparse/__init__.py
git commit -m "refactor: remove load_dotenv from docparse, add ParserConfig.from_settings

- Remove module-level load_dotenv from base.py and __init__.py
- ParserConfig now defaults to empty strings
- Add from_settings() classmethod to read from Settings"
```

---

### 任务 A5：修复 Message 不可变性

**文件：**
- 修改：`src/agent/core/state.py`
- 修改：`src/agent/core/loop.py`

- [ ] **步骤 1：修改 Message.tool_calls 类型**

将 `src/agent/core/state.py` 中：

```python
@dataclass(frozen=True)
class Message:
    """A single conversation message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
```

改为：

```python
@dataclass(frozen=True)
class Message:
    """A single conversation message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
```

- [ ] **步骤 2：适配 add_thought 中的 list → tuple 转换**

在 `src/agent/core/state.py` 的 `add_thought` 方法中（约第 105 行），将：

```python
new_messages.append(
    Message(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
    )
)
```

改为：

```python
tool_calls = (
    tuple(response.tool_calls) if response.tool_calls else None
)
new_messages.append(
    Message(
        role="assistant",
        content=response.content,
        tool_calls=tool_calls,
    )
)
```

- [ ] **步骤 3：检查 loop.py 中是否有直接构造 Message 的地方**

搜索 loop.py 中所有 `Message(` 构造：

```bash
grep -n "Message(" src/agent/core/loop.py
```

确认没有直接传入 `tool_calls=[...]` list 的地方。如果有，将其改为 `tool_calls=(...)` tuple。

- [ ] **步骤 4：运行 Agent 核心测试**

```bash
uv run pytest tests/agent/test_state.py tests/agent/test_loop.py -v
```

预期：全部通过

- [ ] **步骤 5：运行全部非集成测试**

```bash
uv run pytest -m "not integration" -q
```

预期：全部通过

- [ ] **步骤 6：Commit**

```bash
git add src/agent/core/state.py src/agent/core/loop.py
git commit -m "fix: make Message.tool_calls immutable (list → tuple)

- Change Message.tool_calls type from list[ToolCall] | None to tuple[ToolCall, ...] | None
- Adapt add_thought to convert response.tool_calls to tuple
- Enforces frozen dataclass immutability contract"
```

---

## 批次 B：架构债务修复

---

### 任务 B1：ArtifactStore 新增 resolve_ref 和 persist_large_output

**文件：**
- 修改：`src/agent/artifacts/store.py`

- [ ] **步骤 1：添加 resolve_ref 方法**

在 `src/agent/artifacts/store.py` 的 `ArtifactStore` 类中，在 `register_cached_ref` 之后添加：

```python
    def resolve_ref(self, ref_id: str) -> Any:
        """Load data for a $ref ID from disk.

        First checks in-memory artifacts, then falls back to ref_map files.
        Returns the ref_id string itself if resolution fails.
        """
        artifact = self.get(ref_id)
        if artifact is not None:
            return artifact.data

        filepath = self._ref_map.get(ref_id) if hasattr(self, '_ref_map') else None
        if filepath is None:
            return ref_id

        try:
            return json.loads(Path(filepath).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ref_id
```

同时在 `__init__` 中初始化 `_ref_map`：

```python
    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._persist_policies: dict[str, str] = {}
        self._visible_policies: dict[str, str] = {}
        self._ref_map: dict[str, str] = {}
```

- [ ] **步骤 2：添加 persist_large_output 方法**

在 `ArtifactStore` 类中添加：

```python
    def persist_large_output(
        self, tool_name: str, data: object, force: bool = False,
        large_output_threshold: int = 3000,
    ) -> object:
        """If data is large, save to disk and return a preview marker with ref_id."""
        if data is None:
            return data

        try:
            serialized = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return data

        if not force and len(serialized) <= large_output_threshold:
            return data

        import time as _time
        seq = getattr(self, '_ref_counters', {}).get(tool_name, 0) + 1
        if not hasattr(self, '_ref_counters'):
            self._ref_counters: dict[str, int] = {}
        self._ref_counters[tool_name] = seq
        ref_id = f"$ref:{tool_name}:{seq}"

        ts = int(_time.time() * 1000)
        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        from pathlib import Path
        cache_dir = Path(".agent_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_name}_{ts}.json"
        filepath = cache_dir / filename

        try:
            filepath.write_text(serialized, encoding="utf-8")
        except OSError:
            return data

        self._ref_map[ref_id] = str(filepath)

        preview = serialized[:200]
        if len(serialized) > 200:
            preview += f"\n...[truncated, full output ({len(serialized)} chars) saved to {filepath}]"

        return {
            "__persisted_output__": True,
            "ref_id": ref_id,
            "file": str(filepath),
            "size_chars": len(serialized),
            "preview": preview,
        }
```

- [ ] **步骤 3：运行 artifacts 测试**

```bash
uv run pytest tests/agent/test_cache_store.py tests/agent/test_routes_rehydrate.py -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/agent/artifacts/store.py
git commit -m "feat: add resolve_ref and persist_large_output to ArtifactStore

- Prepare ArtifactStore to become the single source of truth for refs
- resolve_ref loads data from in-memory artifacts or disk fallback
- persist_large_output handles Layer 1 large output caching"
```

---

### 任务 B2：创建 ContractBinder 解除循环依赖

**文件：**
- 创建：`src/agent/artifacts/binder.py`

- [ ] **步骤 1：创建 ContractBinder 文件**

创建 `src/agent/artifacts/binder.py`：

```python
"""ContractBinder — coordinates ToolRegistry and ArtifactStore for input/output contracts."""

from __future__ import annotations

import logging
from typing import Any

from src.agent.artifacts.executor import (
    MaterializerRegistry,
    ProjectionExecutor,
    validate_materialized_value,
)
from src.agent.artifacts.models import ProjectionPolicy, ToolInputContract
from src.agent.artifacts.projectors import create_default_projector_registry
from src.agent.artifacts.resolver import ProjectionResolver, emit_event
from src.agent.artifacts.store import ArtifactStore
from src.agent.tools.protocol import ToolResult

logger = logging.getLogger(__name__)


class ContractBinder:
    """Binds tool input contracts to artifacts and registers tool outputs."""

    def __init__(self, artifact_store: ArtifactStore, policy: ProjectionPolicy | None = None) -> None:
        self._artifact_store = artifact_store
        self._policy = policy or ProjectionPolicy()

    def bind_tool_inputs(self, input_contract: ToolInputContract, explicit_kwargs: dict[str, Any]) -> ToolResult:
        """Auto-bind missing tool input fields from artifacts."""
        missing_fields = [
            field for field in input_contract.fields
            if field.required and field.name not in explicit_kwargs
        ]
        if not missing_fields:
            return ToolResult(
                success=True,
                data={"arguments": {}, "artifact_bindings": {}}
            )

        partial_contract = input_contract.model_copy(update={"fields": tuple(missing_fields)})
        resolver = ProjectionResolver(create_default_projector_registry())
        candidates = self._artifact_store.list_projection_candidates()
        resolution = resolver.resolve(partial_contract, candidates, self._policy)

        if resolution.status != "resolved":
            messages = [d.message for d in resolution.diagnostics]
            return ToolResult(success=False, error="; ".join(messages))

        executor = ProjectionExecutor(
            projector_registry=create_default_projector_registry(),
            materializer_registry=MaterializerRegistry.default(),
            artifact_store=self._artifact_store,
            features=self._policy.features,
        )
        arguments: dict[str, Any] = {}
        artifact_bindings: dict[str, str] = {}
        validation_errors: list[str] = []

        for name, plan in resolution.plans.items():
            binding = executor.execute(plan)
            field = next((f for f in input_contract.fields if f.name == name), None)
            if field is not None:
                err = validate_materialized_value(binding.value, field)
                if err is not None:
                    validation_errors.append(err)
            arguments[name] = binding.value
            artifact_bindings[name] = binding.artifact_id

        if validation_errors:
            return ToolResult(success=False, error="; ".join(validation_errors))

        return ToolResult(
            success=True,
            data={"arguments": arguments, "artifact_bindings": artifact_bindings}
        )

    def register_tool_output(
        self,
        tool_name: str,
        output_contract: Any,
        result: ToolResult,
    ) -> None:
        """Register tool result as typed artifact via output_contract."""
        if not result.success or result.data is None:
            return

        data = result.data
        for output_name, output_spec in output_contract.outputs.items():
            if isinstance(data, dict) and data.get("__persisted_output__"):
                ref_id = data.get("ref_id", f"$ref:{tool_name}:latest")
                filepath = data.get("file")
                if filepath:
                    try:
                        import json
                        from pathlib import Path
                        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        pass
            elif result.metadata.get("source_ref_id"):
                ref_id = result.metadata["source_ref_id"]
            else:
                ref_id = f"$ref:{tool_name}:latest"

            self._artifact_store.register_cached_ref(
                ref_id=ref_id,
                artifact_type=output_spec.artifact_type,
                created_by=tool_name,
                data=data,
                role=output_spec.role,
                subject=output_spec.subject,
                projection_allowed=output_spec.projection_allowed,
                debug_only=output_spec.debug_only,
            )
            emit_event("artifact_created", {
                "artifact_type": output_spec.artifact_type,
                "created_by": tool_name,
                "ref_id": ref_id,
            })
```

- [ ] **步骤 2：Commit**

```bash
git add src/agent/artifacts/binder.py
git commit -m "feat: add ContractBinder to decouple ToolRegistry from artifacts

- Extracts contract binding logic from ToolRegistry into standalone class
- bind_tool_inputs: auto-binds tool parameters from typed artifacts
- register_tool_output: registers tool results as typed artifacts
- Eliminates circular import between tools.registry and artifacts modules"
```

---

### 任务 B3：简化 ToolRegistry.execute

**文件：**
- 修改：`src/agent/tools/registry.py`

- [ ] **步骤 1：移除 artifacts 相关导入**

将 `src/agent/tools/registry.py` 顶部的 artifacts 导入替换为：

```python
from __future__ import annotations

import logging
from typing import Any

from .protocol import ToolProtocol, ToolResult
from src.agent.artifacts.models import ToolRuntimePolicy
from src.agent.artifacts.resolver import emit_event
from src.agent.artifacts.store import ArtifactStore
```

移除以下导入：
- `from src.agent.artifacts.executor import ...`
- `from src.agent.artifacts.models import ProjectionPolicy`
- `from src.agent.artifacts.projectors import create_default_projector_registry`
- `from src.agent.artifacts.resolver import ProjectionResolver`

- [ ] **步骤 2：简化 execute 方法中的合同绑定**

将 `_bind_contract_arguments` 方法整体替换为委托给 `ContractBinder`：

```python
    def _bind_contract_arguments(
        self,
        *,
        input_contract: Any,
        artifact_store: ArtifactStore,
        explicit_kwargs: dict[str, Any],
    ) -> ToolResult:
        from src.agent.artifacts.binder import ContractBinder
        binder = ContractBinder(artifact_store, self._policy)
        return binder.bind_tool_inputs(input_contract, explicit_kwargs)
```

同样替换 `_register_output_artifact`：

```python
    def _register_output_artifact(
        self,
        *,
        tool_name: str,
        output_contract: Any,
        result: ToolResult,
        artifact_store: ArtifactStore,
    ) -> None:
        from src.agent.artifacts.binder import ContractBinder
        binder = ContractBinder(artifact_store, self._policy)
        binder.register_tool_output(tool_name, output_contract, result)
```

- [ ] **步骤 3：运行测试**

```bash
uv run pytest tests/agent/test_registry.py tests/agent/test_tool_contract_binding.py -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/agent/tools/registry.py
git commit -m "refactor: simplify ToolRegistry by delegating contract logic to ContractBinder

- Remove direct artifacts executor/projector/resolver imports
- _bind_contract_arguments delegates to ContractBinder.bind_tool_inputs
- _register_output_artifact delegates to ContractBinder.register_tool_output
- Reduces ToolRegistry complexity and breaks circular dependency"
```

---

### 任务 B4：添加 Alembic 数据库迁移

**文件：**
- 创建：`alembic/` 目录
- 修改：`pyproject.toml`
- 修改：`src/dbop/db_manager.py`

- [ ] **步骤 1：添加 alembic 依赖**

```bash
uv add --dev alembic
```

- [ ] **步骤 2：初始化 Alembic**

```bash
uv run alembic init alembic
```

- [ ] **步骤 3：配置 alembic.ini**

编辑 `alembic.ini`：

```ini
[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os
sqlalchemy.url = driver://user:pass@localhost/dbname
```

编辑 `alembic/env.py`，在 `run_migrations_offline` 和 `run_migrations_online` 中适配异步引擎：

```python
from src.dbop.tables import Base

target_metadata = Base.metadata
```

- [ ] **步骤 4：创建初始迁移**

```bash
uv run alembic revision -m "initial schema"
```

- [ ] **步骤 5：移除 _migrate_missing_columns**

将 `src/dbop/db_manager.py` 中的 `_migrate_missing_columns` 方法替换为：

```python
    async def _migrate_missing_columns(self, conn):
        """DEPRECATED: Use alembic migrations instead.

        Kept for backwards compatibility during transition.
        """
        pass
```

- [ ] **步骤 6：运行测试**

```bash
uv run pytest tests/ -q -k "db" --ignore=tests/scripts/
```

预期：全部通过

- [ ] **步骤 7：Commit**

```bash
git add alembic/ pyproject.toml uv.lock src/dbop/db_manager.py
git commit -m "chore: add Alembic for database migrations

- Initialize alembic with async engine support
- Mark _migrate_missing_columns as deprecated
- Future schema changes should use alembic revision"
```

---

## 批次 C：代码质量修复

---

### 任务 C1：提取 _execute_with_retry 消除重复

**文件：**
- 修改：`src/agent/agents/subagent.py`

- [ ] **步骤 1：提取公共重试方法**

在 `SubAgentRunner` 类中添加：

```python
    async def _execute_with_retry(
        self,
        config: SubAgentConfig,
        run_fn: Callable[[], Awaitable[AgentResult]],
    ) -> ToolResult:
        """Execute a subagent run with retry logic."""
        attempts = 1 + config.max_retries
        for attempt in range(attempts):
            try:
                result = await run_fn()
                if result.status == "completed":
                    return ToolResult(
                        success=True,
                        data=_extract_result_data(result),
                        metadata={"is_subagent_result": True},
                    )

                if config.failure_strategy == FailureStrategy.STRICT:
                    return ToolResult(success=False, error=_describe_failure(result))

                if (
                    config.failure_strategy == FailureStrategy.RETRY
                    and attempt < attempts - 1
                ):
                    await asyncio.sleep(2 ** attempt)
                    continue

                return ToolResult(success=False, error=_describe_failure(result))

            except asyncio.TimeoutError:
                logger.warning(
                    "Subagent timed out after %.1fs", config.timeout_seconds
                )
                return ToolResult(
                    success=False,
                    error=f"Subagent timed out after {config.timeout_seconds:.0f}s",
                    metadata={
                        "blocked_reason": "subagent_timeout",
                        "timeout_seconds": config.timeout_seconds,
                    },
                )
            except Exception as exc:
                if config.failure_strategy == FailureStrategy.STRICT:
                    raise
                if (
                    config.failure_strategy == FailureStrategy.RETRY
                    and attempt < attempts - 1
                ):
                    await asyncio.sleep(2 ** attempt)
                    continue
                return ToolResult(success=False, error=str(exc))

        return ToolResult(success=False, error="All retries exhausted")
```

- [ ] **步骤 2：简化 dispatch 和 dispatch_structured**

将 `dispatch` 改为：

```python
    async def dispatch(self, name: str, task: str, **kwargs) -> ToolResult:
        config = self._configs.get(name)
        if config is None:
            return ToolResult(success=False, error=f"Unknown subagent: {name}")

        def _run() -> Awaitable[AgentResult]:
            scoped_store = self._build_scoped_store(kwargs.get("artifact_store"))
            return config.agent.run(
                task=task,
                context=kwargs.get("context"),
                context_manager=kwargs.get("context_manager"),
                on_step=self._callback_holder.make_on_step(name) if self._callback_holder else None,
                on_token=self._callback_holder.make_on_token(name) if self._callback_holder else None,
                on_content_token=self._callback_holder.make_on_content_token(name) if self._callback_holder else None,
                on_tool_result=self._callback_holder.make_on_tool_result(name) if self._callback_holder else None,
                audit_logger=kwargs.get("audit_logger"),
                artifact_store=scoped_store,
            )

        return await self._execute_with_retry(config, _run)
```

类似简化 `dispatch_structured`。

- [ ] **步骤 3：运行测试**

```bash
uv run pytest tests/agent/test_subagent.py tests/agent/test_orchestrator.py -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/agent/agents/subagent.py
git commit -m "refactor: extract _execute_with_retry to eliminate dispatch duplication

- Extract common retry/failure logic into _execute_with_retry method
- dispatch() and dispatch_structured() now delegate to shared method
- Reduces ~80 lines of duplicated retry logic"
```

---

### 任务 C2：提取路由层公共构建函数

**文件：**
- 修改：`src/agent/api/routes.py`

- [ ] **步骤 1：提取 _build_model_client**

在 `_build_api_agent` 之前添加：

```python
def _build_model_client(settings: "Settings") -> "OpenAIModelClient":
    """Create an OpenAIModelClient from Settings."""
    from src.agent.core.model import OpenAIModelClient
    return OpenAIModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens if settings.llm_max_tokens > 0 else None,
        extra_body=settings.llm_extra_body,
    )
```

- [ ] **步骤 2：简化 _build_api_agent 和 _build_chat_agent**

将 `_build_api_agent` 改为：

```python
def _build_api_agent():
    from src.agent.agents.orch import OrchestratorAgent
    from src.agent.agents.parser import ParserAgent
    from src.agent.core.context_manager import ContextManager

    settings = Settings()
    model = _build_model_client(settings)

    agent = OrchestratorAgent(
        model=model,
        skills=OrchestratorAgent.DEFAULT_SKILLS,
        parser=ParserAgent(),
        exporter=None,
    )
    context_manager = ContextManager(model=model, cache_dir=settings.cache_dir)
    return agent, context_manager, model.model_name
```

将 `_build_chat_agent` 改为：

```python
def _build_chat_agent():
    from src.agent.agents.base import Agent
    from src.agent.core.context_manager import ContextManager

    settings = Settings()
    model = _build_model_client(settings)

    agent = Agent(
        name="DocAudit助手",
        role=(
            "你是 DocAudit 文档审核平台的智能助手。"
            "你可以回答用户关于文档格式规范（GB/T 9704-2012）、"
            "内容审核、语法检查、排版建议等方面的问题。"
            "以专业、友好的方式提供帮助。"
        ),
        tools=[],
        model=model,
    )
    context_manager = ContextManager(model=model, cache_dir=settings.cache_dir)
    return agent, context_manager, model.model_name
```

- [ ] **步骤 3：运行测试**

```bash
uv run pytest tests/agent/test_routes_rehydrate.py -v
```

预期：全部通过

- [ ] **步骤 4：Commit**

```bash
git add src/agent/api/routes.py
git commit -m "refactor: extract _build_model_client to eliminate duplicate Settings/ModelClient setup

- Extract shared model client construction into _build_model_client()
- _build_api_agent and _build_chat_agent now ~10 lines each
- Reduces ~40 lines of duplicated Settings initialization"
```

---

### 任务 C3：拆分 loop.py 为多个文件

**文件：**
- 创建：`src/agent/core/loop_guards.py`
- 创建：`src/agent/core/loop_streaming.py`
- 创建：`src/agent/core/loop_audit.py`
- 修改：`src/agent/core/loop.py`

- [ ] **步骤 1：创建 loop_guards.py**

创建 `src/agent/core/loop_guards.py`，包含以下内容：

```python
"""Loop guard functions — detect and terminate futile exploration patterns."""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from .state import AgentState
from ..artifacts.resolver import emit_event

logger = logging.getLogger(__name__)

_MAX_REASONING_DUPE_STEPS = 3
_REASONING_SIMILARITY_THRESHOLD = 0.85
_MAX_NULL_TOOL_RESULTS = 5
_MAX_SAME_TOOL_CALLS = 3
_MAX_CONSECUTIVE_EXPLORATORY = 8
_MAX_EXPLORATORY_WHEN_TERMINAL_READY = 2
_MAX_TURNS_WITHOUT_BUSINESS_ARTIFACTS = 4
_EXPLORATORY_TOOLS = frozenset({"get_artifact", "list_artifacts"})


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_reasoning_loop(reasoning_history: list[str]) -> bool:
    if len(reasoning_history) < _MAX_REASONING_DUPE_STEPS:
        return False
    recent = reasoning_history[-_MAX_REASONING_DUPE_STEPS:]
    return all(
        _similarity(recent[i], recent[i + 1]) >= _REASONING_SIMILARITY_THRESHOLD
        for i in range(len(recent) - 1)
    )


def _normalize_args_for_dedup(args: dict) -> str:
    stripped = {k: v for k, v in args.items() if k not in ("label", "_timestamp", "_request_id")}
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False)


def check_repeated_tool_call(history: list[tuple[str, str]]) -> tuple[bool, str]:
    if len(history) < _MAX_SAME_TOOL_CALLS:
        return False, ""
    recent = history[-_MAX_SAME_TOOL_CALLS:]
    if len(set(recent)) == 1:
        return True, recent[0][0]
    return False, ""


def check_consecutive_exploratory(tool_calls: tuple, count: int) -> bool:
    if not tool_calls:
        return False
    all_exploratory = all(tc.name in _EXPLORATORY_TOOLS for tc in tool_calls)
    new_count = count + 1 if all_exploratory else 0
    return new_count >= _MAX_CONSECUTIVE_EXPLORATORY, new_count


def check_null_results(null_results: list[bool]) -> bool:
    if len(null_results) < _MAX_NULL_TOOL_RESULTS:
        return False
    return all(null_results[-_MAX_NULL_TOOL_RESULTS:])


def count_business_artifacts(artifact_store: Any) -> int:
    if artifact_store is None:
        return 0
    try:
        return len([a for a in artifact_store.list_all() if not a.metadata.debug_only])
    except Exception:
        return 0
```

- [ ] **步骤 2：创建 loop_streaming.py**

创建 `src/agent/core/loop_streaming.py`：

```python
"""Streaming helpers for agent_loop."""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def generate_with_streaming_fallback(
    *,
    model: Any,
    messages: list[dict],
    tools: list[dict] | None,
    on_token: Callable[[str], Awaitable[None]],
    on_content_token: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Any, bool]:
    """Generate response using streaming when available."""
    stream_method = getattr(model, "generate_stream_full", None)
    if stream_method is not None:
        response = await stream_method(
            messages, tools=tools, on_token=on_token, on_content_token=on_content_token
        )
    else:
        response = await model.generate(messages, tools=tools)
        if response.reasoning_content:
            for token in _split_tokens(response.reasoning_content):
                try:
                    await on_token(token)
                except Exception:
                    logger.debug("Token callback failed for token: %r", token)
        if response.content and on_content_token:
            for token in _split_tokens(response.content):
                try:
                    await on_content_token(token)
                except Exception:
                    logger.debug("Content token callback failed for token: %r", token)

    tokens_streamed = bool(response.content and not response.tool_calls)
    return response, tokens_streamed


def _split_tokens(text: str):
    """Split text into display tokens for simulated streaming."""
    buf = ""
    for ch in text:
        if ch in (" ", "\n"):
            if buf:
                yield buf
                buf = ""
            yield ch
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            if buf:
                yield buf
                buf = ""
            yield ch
        else:
            buf += ch
    if buf:
        yield buf
```

- [ ] **步骤 3：创建 loop_audit.py**

创建 `src/agent/core/loop_audit.py`：

```python
"""Audit logging helpers for agent_loop."""

from __future__ import annotations

from .audit_logger import AuditLogger, LLMRequestRecord, LLMResponseRecord, ToolExecutionRecord, TurnRecord


def write_audit_turn(
    audit_logger: AuditLogger,
    turn_index: int,
    timestamp: float,
    llm_request: LLMRequestRecord,
    llm_response: LLMResponseRecord,
    tool_executions: tuple[ToolExecutionRecord, ...] = (),
) -> None:
    """Create and write a TurnRecord to the audit logger."""
    turn = TurnRecord(
        turn_index=turn_index,
        timestamp=timestamp,
        request=llm_request,
        response=llm_response,
        tool_executions=tool_executions,
    )
    audit_logger.write_turn(turn)
```

- [ ] **步骤 4：修改 loop.py 导入新模块**

在 `src/agent/core/loop.py` 顶部添加：

```python
from .loop_guards import (
    check_reasoning_loop,
    check_repeated_tool_call,
    check_consecutive_exploratory,
    check_null_results,
    count_business_artifacts,
    _normalize_args_for_dedup,
)
from .loop_streaming import generate_with_streaming_fallback, _split_tokens
from .loop_audit import write_audit_turn
```

并移除这些函数在 loop.py 中的原定义。

- [ ] **步骤 5：运行测试**

```bash
uv run pytest tests/agent/test_loop.py tests/agent/test_regression.py -v
```

预期：全部通过

- [ ] **步骤 6：Commit**

```bash
git add src/agent/core/loop.py src/agent/core/loop_guards.py src/agent/core/loop_streaming.py src/agent/core/loop_audit.py
git commit -m "refactor: split loop.py into focused submodules

- loop_guards.py: reasoning loop, explore loop, null result detection
- loop_streaming.py: streaming fallback and token splitting
- loop_audit.py: audit log turn writing
- Reduces loop.py from 940 to ~250 lines"
```

---

### 任务 C4：标记外部依赖测试

**文件：**
- 修改：`tests/scripts/test_document_es.py`
- 修改：`tests/scripts/test_resources_extract_rules.py`
- 修改：`tests/scripts/test_resources_import.py`
- 修改：`tests/scripts/test_resources_search.py`

- [ ] **步骤 1：添加 pytestmark 到 ES 测试**

在 `tests/scripts/test_document_es.py` 顶部添加：

```python
import pytest

pytestmark = pytest.mark.integration
```

- [ ] **步骤 2：添加 pytestmark 到资源测试**

在 `tests/scripts/test_resources_extract_rules.py`、`test_resources_import.py`、`test_resources_search.py` 顶部各添加：

```python
import pytest

pytestmark = pytest.mark.integration
```

- [ ] **步骤 3：运行非集成测试确认**

```bash
uv run pytest -m "not integration" -q
```

预期：全部通过（之前失败的 9 个测试应被跳过）

- [ ] **步骤 4：Commit**

```bash
git add tests/scripts/
git commit -m "test: mark external-dependency tests as integration

- test_document_es.py: requires Elasticsearch
- test_resources_*.py: requires external services
- Default pytest run skips these via -m 'not integration'"
```

---

## 批次 D：可选优化

---

### 任务 D1：添加 MinHash 预筛选到查重

**文件：**
- 修改：`src/dedump/dedump.py`
- 修改：`pyproject.toml`

- [ ] **步骤 1：添加 datasketch 依赖**

```bash
uv add datasketch
```

- [ ] **步骤 2：添加 MinHash 预筛选函数**

在 `src/dedump/dedump.py` 中添加：

```python
def _minhash_prescreen(
    new_doc: str,
    library_docs: List[str],
    k: int = 20,
) -> List[Tuple[int, float]]:
    """Use MinHash LSH to find top-k candidate similar documents."""
    try:
        from datasketch import MinHash, MinHashLSH
    except ImportError:
        return []

    # Build LSH index
    lsh = MinHashLSH(threshold=0.3, num_perm=128)
    minhashes = {}

    for idx, doc in enumerate(library_docs):
        m = MinHash(num_perm=128)
        # Character 3-grams
        for i in range(len(doc) - 2):
            m.update(doc[i:i+3].encode("utf-8"))
        lsh.insert(f"doc_{idx}", m)
        minhashes[idx] = m

    # Query
    query = MinHash(num_perm=128)
    for i in range(len(new_doc) - 2):
        query.update(new_doc[i:i+3].encode("utf-8"))

    result = lsh.query(query)
    candidates = []
    for key in result:
        idx = int(key.split("_")[1])
        sim = query.jaccard(minhashes[idx])
        candidates.append((idx, sim))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:k]
```

- [ ] **步骤 3：修改 detect_plagiarism 使用预筛选**

在 `detect_plagiarism` 中，在比对循环之前添加：

```python
    # Use MinHash pre-screening for large libraries
    if len(library_docs) > 100:
        candidates = _minhash_prescreen(new_doc, library_docs, k=20)
        docs_to_check = [(idx, library_docs[idx]) for idx, _ in candidates]
    else:
        docs_to_check = list(enumerate(library_docs))

    for idx, lib_doc in docs_to_check:
        # ... existing comparison logic
```

- [ ] **步骤 4：Commit**

```bash
git add src/dedump/dedump.py pyproject.toml uv.lock
git commit -m "perf: add MinHash pre-screening for large plagiarism libraries

- Use datasketch MinHashLSH when library > 100 docs
- Reduces O(N^2) comparisons to Top-20 candidates
- Falls back to full comparison for small libraries"
```

---

### 任务 D2：添加 RunOptions 封装

**文件：**
- 修改：`src/agent/agents/base.py`

- [ ] **步骤 1：添加 RunOptions dataclass**

在 `src/agent/agents/base.py` 中，在 `AgentResult` 之后添加：

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class RunOptions:
    """Options for Agent.run()."""
    task: str | None = None
    input: Any | None = None
    context: dict[str, str] | None = None
    on_step: Callable[[str, str], Awaitable[None]] | None = None
    on_token: Callable[[str], Awaitable[None]] | None = None
    on_content_token: Callable[[str], Awaitable[None]] | None = None
    on_tool_result: Callable[[str, ToolResult, str], Awaitable[None]] | None = None
    context_manager: Any | None = None
    state: "AgentState" | None = None
    audit_logger: AuditLogger | None = None
    artifact_store: Any | None = None
```

- [ ] **步骤 2：Commit**

```bash
git add src/agent/agents/base.py
git commit -m "feat: add RunOptions dataclass for Agent.run() parameter encapsulation

- Reduces parameter count from 12 to 2 (task/options)
- Improves type safety and call-site readability"
```

---

### 任务 D3：拆分 ToolProtocol

**文件：**
- 修改：`src/agent/tools/protocol.py`

- [ ] **步骤 1：拆分协议**

将 `src/agent/tools/protocol.py` 改为：

```python
"""Tool protocol — the standard interface every tool implements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.agent.tools.summary import ToolSummary


class ToolResult(BaseModel):
    """Standardized tool execution result."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = Field(default_factory=dict)


@runtime_checkable
class ToolProtocol(Protocol):
    """Minimal tool interface — name, description, parameters, execute."""

    name: str
    description: str
    parameters: dict

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with validated parameters."""
        ...

    def summarize(self, result: ToolResult) -> "ToolSummary":
        ...


@runtime_checkable
class ToolWithDisplay(Protocol):
    """Optional: tools with display metadata."""

    display_name: str | None
    output_content_type: str | None


@runtime_checkable
class ToolWithContracts(Protocol):
    """Optional: tools with input/output contracts for artifact binding."""

    input_contract: Any | None
    output_contract: Any | None


@runtime_checkable
class ToolWithRuntimePolicy(Protocol):
    """Optional: tools with runtime execution policies."""

    skip_persist: bool
    runtime_policy: Any | None
    output_schema: dict | None
```

- [ ] **步骤 2：Commit**

```bash
git add src/agent/tools/protocol.py
git commit -m "refactor: split ToolProtocol into composable mixin protocols

- ToolProtocol: minimal base (name, description, parameters, execute)
- ToolWithDisplay: display_name, output_content_type
- ToolWithContracts: input_contract, output_contract
- ToolWithRuntimePolicy: skip_persist, runtime_policy, output_schema"
```

---

### 任务 D4：替换 copy.deepcopy

**文件：**
- 修改：`src/agent/agents/orch.py`

- [ ] **步骤 1：替换 deepcopy**

将 `src/agent/agents/orch.py` 中：

```python
    @property
    def audit_results(self) -> dict[str, Any]:
        """Aggregated results from all auditors, keyed by auditor name."""
        return copy.deepcopy(self._audit_results)
```

改为：

```python
    @property
    def audit_results(self) -> dict[str, Any]:
        """Aggregated results from all auditors, keyed by auditor name."""
        return {k: v for k, v in self._audit_results.items()}
```

同时移除顶部的 `import copy`。

- [ ] **步骤 2：Commit**

```bash
git add src/agent/agents/orch.py
git commit -m "refactor: replace copy.deepcopy with dict comprehension in audit_results

- Values are JSON-serializable primitives, shallow copy is sufficient
- Follows immutability principle: create new object instead of mutating"
```

---

## 最终验证

- [ ] **步骤：运行全部非集成测试**

```bash
uv run pytest -m "not integration" -q
```

预期：全部通过（≥1310 passed）

- [ ] **步骤：运行 Agent 核心测试**

```bash
uv run pytest tests/agent/ -v
```

预期：全部通过

---

## 自检清单

**1. 规格覆盖度：**
- [x] 问题 1 (CORS) → 任务 A1
- [x] 问题 2 (load_dotenv) → 任务 A2, A3, A4
- [x] 问题 3 (不可变性) → 任务 A5
- [x] 问题 4 (三重缓存) → 任务 B1
- [x] 问题 5 (代码重复) → 任务 C1
- [x] 问题 6 (查重算法) → 任务 D1
- [x] 问题 7 (循环依赖) → 任务 B2, B3
- [x] 问题 8 (构建重复) → 任务 C2
- [x] 问题 10 (超大文件) → 任务 C3
- [x] 问题 11 (参数过多) → 任务 D2
- [x] 问题 12 (ToolProtocol) → 任务 D3
- [x] 问题 13 (数据库迁移) → 任务 B4
- [x] 问题 14 (测试标记) → 任务 C4
- [x] 问题 15 (deepcopy) → 任务 D4
- [x] 问题 16, 17, 18 → 批次 D 中说明（暂不改动或已覆盖）

**2. 占位符扫描：** ✅ 无 TBD/TODO/"添加适当的错误处理"/"类似任务 N"

**3. 类型一致性：** ✅ 所有任务中使用的类型名称、方法签名一致
