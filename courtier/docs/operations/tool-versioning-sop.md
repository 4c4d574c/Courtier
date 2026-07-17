# Courtier 工具版本化标准作业流程（SOP）

> 本文档配套 `pi-architecture-migration-plan.md` Phase 4 的工具协议版本化能力，给出插件/工具升级、废弃、回滚的操作规范。

---

## 1. 适用范围

- 所有注册到 `ToolRegistry` 的插件工具；
- 所有通过 `CapabilityRegistry` 暴露的 `tool` 类型能力；
- 涉及参数 schema、输出格式、调用语义变更的升级。

---

## 2. 版本声明

工具通过实现 `ToolVersioned` 协议声明版本信息：

```python
from courtier.agent.tools.protocol import ToolVersioned

class MyTool:
    name = "my_tool"
    description = "..."
    parameters = {...}

    version = "1.1.0"
    api_version = "1.0"
    deprecated = False
    replaced_by = None
```

规则：

- `version`：语义化版本，同 `api_version` 内出现不兼容 schema 变更时必须升主版本；
- `api_version`：契约版本，仅当工具对外的 OpenAI function schema 发生破坏性变更时升级；
- `deprecated`：标记为 True 后，该版本不再被 LLM schema 暴露，但旧调用仍可被显式版本调用；
- `replaced_by`：填写替代版本的 `version` 字符串，用于日志与前端提示。

---

## 3. 新版本上线流程

1. **开发侧**
   - 复制旧工具实现，提升 `version`；
   - 如需破坏性变更，同时提升 `api_version`；
   - 保留旧版本注册至少一个发布周期。

2. **注册侧**
   - 在插件初始化时依次注册旧版本和新版本：

   ```python
   registry.register(MyToolV1())
   registry.register(MyToolV2())
   ```

   - `ToolRegistry` 内部按 `_versions[name][version]` 存储，`get(name)` 返回最新非废弃版本。

3. **Schema 暴露**
   - `ToolRegistry.get_schemas(include_deprecated=False)` 默认只向 LLM 暴露最新非废弃版本；
   - 如需灰度，可在 `meta` 中配置 `expose_to_llm=false`，通过内部 API 显式调用。

4. **验证**
   - 运行契约测试，确保 `ToolInfo` 与 tool 实际 `parameters` 一致；
   - 在 staging 运行 24 小时，观察 `GUARDRAIL_BLOCKED_TOTAL` 和 `TOOL_EXECUTIONS_TOTAL`。

---

## 4. 废弃与下线流程

| 阶段 | 操作 | 观察周期 |
|---|---|---|
| 1 | 新版本稳定后，旧版本标记 `deprecated=True`，填写 `replaced_by` | 1 周 |
| 2 | 监控旧版本调用量；若降为 0，进入阶段 3 | 1 周 |
| 3 | 调用 `registry.unregister(name, version="x.y.z")` 下线旧版本 | - |
| 4 | 若下线后报错激增，立即重新注册旧版本并回滚调用方 | - |

**注意**：`api_version` 升级产生的旧版本必须保留到所有历史会话消息中的 tool call 都已过期或会话已归档。

---

## 5. 回滚策略

- **配置回滚**：通过 `Settings.agent_runtime` 关闭新能力（如把 `tool_layer` 切为 `log` 观察影响）；
- **版本回滚**：重新注册旧版本，并将旧版本 `deprecated` 设为 False，新版本设为 True；
- **代码回滚**：保留旧工具源码在 `plugins/<domain>/<tool>/versions/` 目录，确保可在不重新发布插件的情况下切换。

---

## 6. 监控指标

关注以下 Prometheus 指标：

- `tool_executions_total{tool_name="...", status="..."}`：各版本调用量；
- `capability_registry_size{capability_type="tool"}`：注册工具版本总数；
- `guardrail_blocked_total{layer="tool", guard_name="..."}`：版本化后是否触发工具层护栏。

---

## 7. 前端提示

当 LLM schema 中的工具描述包含 `[DEPRECATED]` 时，前端调试面板应高亮显示，并展示 `replaced_by` 字段。普通用户界面保持默认显示最新非废弃版本。
