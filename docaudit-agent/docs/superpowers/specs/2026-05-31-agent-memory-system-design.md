# Agent 记忆系统设计文档

**日期**: 2026-05-31  
**状态**: 待实现  
**关联模块**: `src/agent/memory/`, `src/agent/agents/base.py`, `src/agent/api/`

---

## 1. 概述

本文档定义 DocAudit 审核助手的跨会话长期记忆系统的设计。该系统在现有三层上下文压缩（ContextManager）和会话持久化（SessionStore）之上，新增**长期记忆层**，使 Agent 能够：

- 记住用户的审核偏好（如忽略某类格式警告）
- 积累领域知识和文档类型模式
- 在每次审核开始时自动检索相关记忆并注入系统提示

## 2. 背景与现状

当前系统已有以下与"记忆"相关的模块：

| 模块 | 职责 | 问题 |
|------|------|------|
| `AgentState.messages` | 当前会话的对话历史 | 会话结束即丢弃 |
| `ContextManager` | 三层上下文压缩（大输出持久化、旧结果替换、全量摘要）| 仅作用于会话内 |
| `SessionStore` | 会话记录持久化到 JSON 文件 | 供历史查询，不被 Agent 主动利用 |
| `FileMemoryStore` | 基于文件的键值存储 | 空壳实现，未被实际使用 |

**核心缺口**: 没有跨会话的知识提炼和检索机制。

## 3. 设计目标

| 目标 | 说明 |
|------|------|
| 纯隐式写入 | 每次审核会话结束后自动提炼记忆，无需用户干预 |
| 语义+结构化检索 | 用向量相似度召回候选，再用结构化字段过滤 |
| 三层维度隔离 | 用户偏好（按用户隔离）、领域知识（全局共享）、文种模式（按文种隔离）|
| 故障降级 | 记忆层任何故障不影响主审核流程 |
| 零冲突集成 | 保留 ContextManager 和 SessionStore 的现有职责 |

## 4. 架构设计

采用**分层记忆架构**：

```
┌─────────────────────────────────────────────────────────────┐
│  工作记忆 (Working Memory)                                   │
│  AgentState.messages ──► ContextManager 三层压缩           │
│  [生命周期: 单次会话]                                        │
└─────────────────────────────────────────────────────────────┘
                              ▼ session.end()
┌─────────────────────────────────────────────────────────────┐
│  短期记忆 (Short-term Memory)                                │
│  SessionStore ──► JSON 文件持久化                            │
│  [生命周期: 数天~数周，供历史查询和提炼原料]                  │
└─────────────────────────────────────────────────────────────┘
                              ▼ 异步提炼 (SessionSummarizer)
┌─────────────────────────────────────────────────────────────┐
│  长期记忆 (Long-term Memory)                                 │
│  MemoryIndex(ES) ──► dense_vector + metadata                 │
│  [生命周期: 永久，语义检索]                                  │
└─────────────────────────────────────────────────────────────┘
```

## 5. 核心组件

```
src/agent/
├── memory/
│   ├── __init__.py
│   ├── models.py          # MemoryEntry, MemoryType, MemoryScope
│   ├── indexer.py         # MemoryIndexer — 写入长期记忆
│   ├── retriever.py       # MemoryRetriever — 语义+结构化检索
│   ├── extractor.py       # SessionSummarizer — 从会话提炼记忆
│   └── es_client.py       # ES 向量操作封装
├── agents/base.py         # Agent.run() 集成记忆检索
└── api/routes.py          # /api/v1/memory/* 管理接口
```

| 组件 | 职责 | 依赖 |
|------|------|------|
| `MemoryEntry` | 记忆条目的 Pydantic 数据模型 | pydantic |
| `MemoryIndexer` | 将提炼后的记忆写入 ES，处理 embedding 生成 | ESClient, ModelClient |
| `MemoryRetriever` | 根据上下文检索相关记忆，返回排序后的条目列表 | ESClient |
| `SessionSummarizer` | 从 SessionRecord 提炼关键知识，生成 MemoryEntry 候选 | ModelClient |
| `ESClient` | 封装 ES dense_vector 索引操作（index, search, delete）| elasticsearch-py |

## 6. 数据模型

### 6.1 MemoryType

```python
class MemoryType(str, Enum):
    PREFERENCE = "preference"      # 用户偏好
    DOMAIN = "domain"              # 领域知识
    PATTERN = "pattern"            # 文档类型模式
```

### 6.2 MemoryScope

```python
class MemoryScope(BaseModel):
    user_id: str | None = None     # null = 全局共享
    doc_type: str | None = None    # null = 通用
    memory_type: MemoryType
```

### 6.3 MemoryEntry

```python
class MemoryEntry(BaseModel):
    id: str                        # UUID
    content: str                   # 自然语言描述
    embedding: list[float] | None  # 768 维向量
    memory_type: MemoryType
    scope: MemoryScope
    confidence: float = 0.5        # 提炼置信度 (0-1)
    source_session_id: str | None
    access_count: int = 0          # 检索命中次数
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None    # 可选过期
```

## 7. ES 索引设计

索引名: `agent_memories`

```json
{
  "mappings": {
    "properties": {
      "content": {
        "type": "text",
        "analyzer": "ik_smart"
      },
      "embedding": {
        "type": "dense_vector",
        "dims": 768,
        "index": true,
        "similarity": "cosine"
      },
      "memory_type": { "type": "keyword" },
      "scope.user_id": { "type": "keyword" },
      "scope.doc_type": { "type": "keyword" },
      "confidence": { "type": "float" },
      "access_count": { "type": "integer" },
      "created_at": { "type": "date" }
    }
  }
}
```

### 检索 DSL 示例

```json
{
  "bool": {
    "must": [
      {
        "knn": {
          "field": "embedding",
          "query_vector": [...],
          "k": 20,
          "num_candidates": 100
        }
      }
    ],
    "filter": [
      { "terms": { "memory_type": ["preference", "domain"] } },
      {
        "bool": {
          "should": [
            { "term": { "scope.user_id": "user_123" } },
            { "bool": { "must_not": { "exists": { "field": "scope.user_id" } } } }
          ]
        }
      },
      {
        "bool": {
          "should": [
            { "term": { "scope.doc_type": "通知" } },
            { "bool": { "must_not": { "exists": { "field": "scope.doc_type" } } } }
          ]
        }
      }
    ]
  }
}
```

**关键设计点**:
- `scope.user_id` / `scope.doc_type` 为 null 表示全局共享
- `should + must_not exists` 同时命中用户私有 + 全局共享记忆
- `confidence < 0.3` 的记忆不入库
- `access_count` 用于后续权重优化

## 8. 数据流

### 8.1 提炼管道

```
SessionStore(JSON)
       │
       ▼
SessionSummarizer (LLM-based)
  - 输入: SessionRecord (steps, tools, verdict)
  - 输出: MemoryEntry 候选列表
       │
       ▼
MemoryIndexer
  - 语义去重 (向量相似度)
  - 冲突检测
  - 写入 ES
       │
       ▼
ES agent_memories
```

**SessionSummarizer Prompt 框架**:

```
你是公文审核系统的记忆提炼助手。请分析以下审核会话记录，
提取值得长期保存的知识（用户偏好、领域发现、文档模式）。
输出 JSON 数组，每个元素包含：
- content: 记忆的自然语言描述
- memory_type: "preference" | "domain" | "pattern"
- scope.user_id: 用户ID（仅 preference 类）
- scope.doc_type: 文种（通知/函/请示等）
- confidence: 0.0-1.0 置信度
```

### 8.2 检索管道

```
Agent.run(task, context)
       │
       ▼
MemoryRetriever
  - task 文本生成 embedding
  - ES kNN + bool filter
  - 相关性排序（语义 + 时间 + 访问频率加权）
       │
       ▼
System Prompt 记忆段注入
  [相关记忆]
  1. xxx
  2. xxx
```

**注入位置**: `Agent.build_system_prompt()` 中，在 identity 和 tools 之间插入 `MemorySection`。

## 9. 记忆更新机制

### 9.1 语义去重 + 合并更新 + 冲突降级

```
新记忆 ──► 检索相似记忆 ──► 判断
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
相似度<0.85  0.85-0.95    >0.95
    │           │           │
    ▼           ▼           ▼
创建新条目   合并更新      覆盖更新
```

### 9.2 更新规则表

| 场景 | 行为 |
|------|------|
| 语义相似但不矛盾 (0.85-0.95) | `content` 加权融合，`confidence` 取加权平均，`access_count += 1` |
| 高度相似 (>0.95) | 直接更新 `content`，`confidence = max(old, new)` |
| 内容矛盾 | 旧记忆 `confidence *= 0.7`，新记忆以原 confidence 入库 |
| 用户偏好改变 | 旧偏好 `expires_at = now() + 7d`，新偏好入库 |

### 9.3 去重逻辑伪代码

```python
async def _deduplicate(self, entry: MemoryEntry) -> MemoryEntry:
    candidates = await self.retriever.search(
        query_embedding=entry.embedding,
        scope=entry.scope,
        top_k=5,
    )
    for candidate in candidates:
        similarity = cosine_similarity(entry.embedding, candidate.embedding)
        if similarity > 0.95:
            return self._merge_update(candidate, entry, strategy="override")
        elif similarity > 0.85:
            if self._is_contradictory(entry.content, candidate.content):
                await self._deprecate(candidate)
            else:
                return self._merge_update(candidate, entry, strategy="blend")
    return entry  # 创建新条目
```

## 10. 与现有系统集成

### 10.1 改动清单

| 文件 | 改动类型 | 具体内容 |
|------|---------|---------|
| `src/agent/agents/base.py` | 修改 | `Agent.run()` 中 `build_system_prompt()` 前插入记忆检索 |
| `src/agent/api/routes.py` | 新增 | `POST /api/v1/memory/query` 等管理接口 |
| `src/agent/memory/` | 新增 5 个文件 | `models.py`, `indexer.py`, `retriever.py`, `extractor.py`, `es_client.py` |
| `src/agent/api/session_store.py` | 修改 | 会话完成后可选触发提炼 |
| `src/agent/core/loop.py` | 无改动 | 保持不变 |
| `src/agent/core/context_manager.py` | 无改动 | 保持不变 |

### 10.2 Agent.run() 集成伪代码

```python
async def run(self, task, context=None, ..., state=None):
    memory_section = ""
    if self.memory and context:
        memories = await self.memory.retrieve(
            query=task,
            user_id=context.get("user_id"),
            doc_type=context.get("doc_type"),
            top_k=5,
        )
        if memories:
            memory_section = self._format_memory_section(memories)

    system_prompt = self.build_system_prompt(context)
    if memory_section:
        system_prompt = self._inject_memory_section(system_prompt, memory_section)
    # ... 其余逻辑不变
```

### 10.3 API 新增接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/memory` | 列出用户的记忆条目 |
| `DELETE` | `/api/v1/memory/{id}` | 删除某条记忆 |
| `POST` | `/api/v1/memory/query` | 语义搜索记忆 |

## 11. 错误处理

| 场景 | 降级策略 |
|------|---------|
| ES 连接失败 | 记忆功能降级：检索返回空，写入跳过，不影响主审核流程 |
| Embedding 服务失败 | 降级为关键词检索（content 字段的 text match）|
| 提炼 LLM 失败 | 跳过本次提炼，不阻塞会话完成 |
| 去重计算失败 | 直接创建新条目（允许少量重复）|
| 记忆注入的 prompt 超长 | 按 relevance 截断，只取前 N 条 |

**核心原则**: 记忆系统是**增强层**，任何故障都不阻断主审核流程。

## 12. 测试策略

### 12.1 测试覆盖

| 层级 | 测试文件 | 覆盖内容 |
|------|---------|---------|
| 单元测试 | `test_models.py` | MemoryEntry 序列化、MemoryScope 过滤 |
| 单元测试 | `test_extractor.py` | SessionSummarizer prompt 构建、JSON 解析 |
| 单元测试 | `test_retriever.py` | ES DSL 查询构建、结果排序（mock）|
| 单元测试 | `test_indexer.py` | 去重逻辑、相似度计算 |
| 集成测试 | `test_es_integration.py` | 真实 ES 容器：写入 → 检索 → 更新 → 删除 |
| 集成测试 | `test_end_to_end.py` | 完整流程：session → 提炼 → 检索 → 注入 |

### 12.2 关键测试场景

1. 用户偏好隔离：用户 A 的记忆不会返回给用户 B
2. 全局记忆共享：domain 类记忆对所有用户可见
3. 语义去重：相似度 0.92 时合并，0.98 时覆盖
4. 矛盾降级：新旧偏好矛盾时旧偏好 confidence 下降
5. ES 降级：ES 不可用时主流程不受影响
6. Embedding 降级：embedding 失败时回退 text match

**覆盖率要求**: ≥ 80%

## 13. 已决断事项

| 事项 | 决断 | 说明 |
|------|------|------|
| Embedding 模型选型 | **B** | 使用本地轻量嵌入模型（已有部署）|
| 记忆清理策略 | **B** | 每日定期任务，删除条件：①expires_at 已过期 ②confidence<0.1 且 access_count=0 且创建>30天 ③confidence<0.05 |
| ES 索引初始化 | **C** | 应用启动自动创建 + 独立脚本支持 force_recreate |

---

**作者**: Claude  
**评审状态**: 待用户评审
