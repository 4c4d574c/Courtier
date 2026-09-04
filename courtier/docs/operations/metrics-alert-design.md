# Courtier Agent Runtime Metrics 与 Alert 设计

> 本文档定义 Courtier 迁移到 Pi 架构后需要持续观测的核心指标与告警规则，覆盖 Phase 1–4 的关键能力。

---

## 1. 指标命名规范

所有指标统一前缀 `courtier_`，按 Prometheus 最佳实践命名：

```text
courtier_<component>_<metric>_<unit>
```

- Counter：`<metric>_total`
- Histogram：`<metric>_<unit>`（seconds / bytes / tokens）
- Gauge：`<metric>`

---

## 2. 已落地指标清单

### 2.1 Phase 1 — 事件总线

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `event_bus_dropped_total` | Counter | `event_type`, `strategy` | 背压丢弃事件数 |

**使用位置**：`courtier/agent/core/event_bus.py`

### 2.2 Phase 2 — LLM 抽象与状态机

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `courtier_model_router_fallback_total` | Counter | `from_backend`, `to_backend`, `reason` | 模型降级事件 |
| `courtier_state_machine_invalid_total` | Counter | `from_status`, `to_status` | 非法状态转换（非严格模式） |

**使用位置**：
- `courtier/agent/core/backends/router.py`
- `courtier/agent/core/state_machine.py`

### 2.3 Phase 3 — 扩展系统与记忆

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `courtier_capability_registry_size` | Gauge | `capability_type` | 各类型能力注册数 |
| `plugin_lifecycle_restart_total` | Counter | `provider`, `outcome` | 插件重启/失败/致命事件 |

**使用位置**：
- `courtier/agent/core/capability.py`
- `courtier/plugin/lifecycle.py`

### 2.4 Phase 4 — 护栏与会话树

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `guardrail_blocked_total` | Counter | `layer`, `guard_name` | 护栏 block 动作 |
| `courtier_conversation_tree_branches` | Gauge | `session_id` | 会话树叶节点数 |

**使用位置**：
- `courtier/agent/core/guardrails/guardrail_system.py`
- `courtier/agent/api/services/stream_service.py`

### 2.5 既有指标

`courtier/agent/telemetry/metrics.py` 中已有：

- `agent_requests_total`
- `llm_calls_total`
- `llm_token_usage_total`
- `tool_executions_total`
- `subagent_dispatch_total`
- `agent_latency_seconds`
- `tool_latency_seconds`
- `plugin_state_changes`

---

## 3. 推荐 Alert 规则

### 3.1 P1 — 服务可用性

```yaml
- alert: CourtierModelRouterFallbackSpike
  expr: rate(courtier_model_router_fallback_total[5m]) > 0.5
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "模型路由降级频率异常"
    description: "{{ $value }} fallback/s，主模型可能不可用。"

- alert: CourtierPluginFatal
  expr: increase(plugin_lifecycle_restart_total{outcome="fatal"}[5m]) > 0
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "插件 {{ $labels.provider }} 进入 FATAL 状态"
```

### 3.2 P2 — 质量与误杀

```yaml
- alert: CourtierGuardrailBlockSpike
  expr: rate(guardrail_blocked_total[5m]) > 0.3
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "护栏 block 频率突增"
    description: "层 {{ $labels.layer }} / {{ $labels.guard_name }} 可能误杀。"

- alert: CourtierEventBusDrops
  expr: rate(event_bus_dropped_total[5m]) > 0
  for: 1m
  labels:
    severity: warning
  annotations:
    summary: "事件总线出现背压丢弃"
```

### 3.3 P3 — 运营观测

```yaml
- alert: CourtierConversationTreeBranchesHigh
  expr: courtier_conversation_tree_branches > 20
  for: 10m
  labels:
    severity: info
  annotations:
    summary: "会话分支数过多，建议引导用户归档"
```

---

## 4. Dashboard 建议

### Grafana 面板

1. **Agent Overview**：请求量、latency、token 消耗；
2. **Event Bus Health**：队列深度（需额外暴露）、drop 率；
3. **Model Routing**：fallback 次数、各 backend 调用占比；
4. **Guardrails**：各层 block/log 分布；
5. **Plugins**：状态分布、重启次数；
6. **Conversation Tree**：平均分支数、fork/rewind API QPS。

---

## 5. 后续可补充指标

- `event_bus_queue_size`：各订阅者队列长度（Gauge）；
- `courtier_memory_recall_latency_seconds`：MemoryManager retrieval 层延迟；
- `courtier_subagent_event_scope_suppressed_total`：blackbox 模式下被抑制的事件数；
- `tool_version_mismatch_total`：显式版本调用失败的次数。
