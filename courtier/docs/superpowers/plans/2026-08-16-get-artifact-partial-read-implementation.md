# get_artifact 长文档部分读取（大纲导航）实现计划

> **For agentic workers:** 按 phase 逐个 Task 推进，每个 Task 用 checkbox（`- [x]`）跟踪，完成后按 `courtier/CLAUDE.md` 约定立即以约定式提交提交。禁止 `git add -A`（根仓库含独立仓库 `pi/`）。

**Goal:** 让模型面对长文档的 `$ref` 结果时能"先看大纲、再定点取节"，替代现在的盲翻页（`chunk_index`）和行级关键词摘录（`query`）。两步走：先给 `get_artifact` 增加 `outline`/`section` 结构化读取参数（核心能力），再在结果摘要层自动透出文档大纲（减少一次往返）。

**Architecture:** 新增一个纯函数 markdown 结构解析器（`artifacts/outline.py`，无依赖、无网络），供两处消费：`tools/builtin/get_artifact.py` 的结构化读取分支（`outline`/`section` 参数）和 `runtime/summarizer.py` 的摘要增强（markdown 类结果的 summary 附带大纲行）。不新增工具、不改插件、不动持久化格式。

**Tech Stack:** Python 3.12+、uv、pytest。

**关联文档:** `docs/superpowers/plans/2026-08-16-citation-alignment-and-ref-utilization-implementation.md`（registry-first 路由、`$ref` 文本适配由该计划引入；本计划的 outline/section 参数必须兼容那条直达路径）。

---

## 背景与现状问题

上传长文档 → `convert_document` → markdown 持久化为 `$ref` 后，模型想读其中一部分时的现状：

| 现有手段 | 实现 | 硬伤 |
|----------|------|------|
| `get_artifact(query=...)` | `cache_store.py:350` 行级子串匹配，命中行拼接 | 丢上下文；"读第三章"只能命中含"第三章"的行，拿不到整节；无命中静默退化为前 N 字符截断 |
| `get_artifact(chunk_index=N)` | 固定 token 窗口切片 | 盲翻页——模型不知道第 N 页有什么，只能顺序翻 |
| preview/key_excerpts | 前几百字 + 采样摘录 | 无结构视图，模型不知道文档有哪些章节，无从决定读哪 |

缺的不是"读部分内容"的能力，而是**导航**：先看目录，再定点取节。

**目标形态：** `get_artifact(id=$ref, outline=true)` 返回结构化大纲（标题、层级、长度、起始位置）；`get_artifact(id=$ref, section="一、项目背景")` 或 `section="3"` 返回该节完整内容（含子节、超长截断并标注）。摘要层对 markdown 结果自动附大纲首屏，多数场景少一次往返。

**关键约束（上一轮改动引入，必须兼容）:** `get_artifact` 已有 registry-first 直达路由（registry 命中且无 `artifact_type` 时直接返回 `artifact.data`）——`outline`/`section` 参数存在时必须**绕过直达**进入结构化读取；`$ref` 文本字段适配（markdown/text/content…）复用同一张优先级表提取待解析文本。

---

## 文件职责

| 文件 | 职责 | 操作 | 阶段 |
|------|------|------|------|
| `courtier/agent/artifacts/outline.py` | markdown/公文结构解析：文本 → 节列表（标题、层级、起止、长度）；大纲渲染；节定位 | **新建** | 0 |
| `courtier/agent/tools/builtin/get_artifact.py` | `outline`/`section` 参数；结构化读取分支（registry 命中与 `$ref` read 两条路径共用） | 修改 | 0 |
| `courtier/agent/runtime/summarizer.py` | markdown 类结果的 summary 附大纲首屏 | 修改 | 1 |
| `courtier/prompts/defaults/{zh-CN,en-US}/behavioral.yaml` | 长文档读取指引（先大纲后定节） | 修改 | 1 |
| `tests/courtier/test_outline.py` | 解析器单测 | **新建** | 0 |
| `tests/agent/tools/test_get_artifact.py` | outline/section 集成测试 | 修改 | 0 |
| `tests/agent/runtime/test_summarizer.py` | 摘要大纲断言 | 修改 | 1 |

> 资源库文档继续走 `read_chunks`（ES 坐标系），本计划只覆盖 `$ref` 上传文档/工具结果，两套坐标系保持分离。

---

## 设计决策（已确认）

1. **切分策略**：双通道识别——markdown ATX 标题（`#{1,6}`）与公文层级文本（`第X章`/`第X节`/`一、`/`（一）`）统一映射为层级 level；`第X条` 不进大纲（太细，公文条目动辄上百）。无标题文档退化为固定字符窗口（4000 字符）的"块 1/N"伪目录。
2. **section 匹配语义**：精确标题匹配 → 大纲序号（`"3"` 或 `"3."`）→ 标题包含匹配，按序取第一个命中；结果恒带 `matched_title` 便于模型自检。
3. **节内容范围**：从该标题行起，到下一个同级或更高级标题之前，含全部子节。
4. **超长节**：按 `max_tokens`（默认 2000，约 4 字符/token）截断，结果带 `"truncated": true` 与 `section_chars`（节原始长度），引导模型再缩范围。
5. **返回结构**：`outline=true` → `{"outline": [{"index", "level", "title", "start_char", "chars"}], "total_chars"}`；`section` → `{"section_title", "content", "truncated", "section_chars"}`。
6. **摘要大纲**：只对含文本字段（`_TEXT_FIELD_PRIORITY`）且解析出 ≥2 节的结果附加；附加在 summary 字符串末尾（不占 key_excerpts 预算），最多 ~10 个标题、500 字符封顶。

---

## Phase 0：结构化读取核心（解析器 + get_artifact 参数）

### Task 0.1: markdown/公文结构解析器

**Files:** `courtier/agent/artifacts/outline.py`、`tests/courtier/test_outline.py`

- [x] **Step 1:** 解析器核心（纯函数，stdlib only）：

  ```python
  @dataclass(frozen=True)
  class Section:
      index: int          # 1-based 大纲序号（返回给模型的 section 序号）
      level: int          # 1=章/#, 2=节/##/一、, 3=（一）/###
      title: str
      start_char: int
      end_char: int       # 下一个同级或更高级标题的起始（或文末）
      chars: int

  def parse_sections(text: str) -> list[Section]
  ```

  识别规则（行级扫描）：
  - ATX：`^(#{1,6})\s+(.+)` → level = min(len(#), 3)；
  - 公文：`^第[一二三四五六七八九十百千0-9]+章` → L1；`^第…+节` → L2；`^[一二三四五六七八九十]+、` → L2；`^（[一二三四五六七八九十]+）` 或 `^\(...\)` 全角括号 → L3；
  - 混合层级归一：ATX level 与公文 level 共存时各自保留，同 level 相遇即结束上一节；
  - 无标题：全文按 4000 字符窗口切成 `块 1`、`块 2`…（level 1），保证 outline 永远非空。
- [x] **Step 2:** 节定位：

  ```python
  def find_section(sections: list[Section], selector: str) -> Section | None
  ```

  匹配序：标题精确相等 → 序号（剥离 `.`、`第`、`节` 等装饰后数字/中文数字比较 index）→ 标题包含 selector。
- [x] **Step 3:** 单测（`test_outline.py`）：ATX 层级嵌套归属；公文层级（章/节/一、/（一））；混合文档；无标题退化块数与区间正确；selector 三种匹配各命中；中文数字序号（"三" → index 3）。

### Task 0.2: get_artifact 的 outline/section 参数

**Files:** `courtier/agent/tools/builtin/get_artifact.py`、`tests/agent/tools/test_get_artifact.py`

- [x] **Step 1:** 参数 schema 增补（description 写明用途与互斥关系）：

  ```python
  "outline": {"type": "boolean", "description": "返回文档结构化大纲（标题/层级/长度/位置），长文档先用它定位再按 section 读取，默认 false"},
  "section": {"type": "string", "description": "按大纲序号或标题文本读取指定节的完整内容（含子节），与 outline 配合用"},
  ```
- [x] **Step 2:** 结构化读取分支（优先级：outline/section 存在 → 结构化；否则维持现有路由）：
  - 文本提取：data 为 dict 时按 `_TEXT_FIELD_PRIORITY`（markdown/text/content/plain_text/chunk_text/data）取文本；data 为 str 直接用；取不到 → `success=False` + "该结果不含可解析的文本内容"。
  - 两条现存路径都接入：registry-first 直达（`artifact.data`）与 `_resolve_ref`（read 结果）——抽公共 helper `_structured_read(data, outline, section, max_tokens)`。
  - `outline=true` → `parse_sections` → 返回大纲 dict（见设计决策 5）；`section` → `find_section` → 切片 `[start_char:end_char]` → `max_tokens` 截断 + `truncated`/`section_chars` 标注；未命中 → `success=False` + 列出前 10 个可选节标题供重选。
- [x] **Step 3:** 工具 description 更新：`get_artifact` 主描述补一句"长文档建议先 outline=true 看大纲，再用 section 定点读取"。
- [x] **Step 4:** 集成测试（`test_get_artifact.py`）：
  - persist 一份长 markdown（含 `#`/`第X章`/`一、`混合标题）→ `outline=true` 返回全部节；
  - `section="一、项目背景"`（标题匹配）、`section="3"`（序号）各取到正确区间；
  - 未命中 → 错误信息含候选标题列表；
  - registry-first 路径（未持久化 inline artifact）同样支持 outline/section；
  - 无 `outline`/`section` 时现有行为逐字节不变（回归既有测试即覆盖）。

**验收（Phase 0）:** 新单测 + 集成测试全绿；`uv run pytest tests/agent/tools/ tests/courtier -q` 无回归。

---

## Phase 1：摘要透出大纲 + 行为指引

### Task 1.1: summarizer 对 markdown 结果附大纲首屏

**Files:** `courtier/agent/runtime/summarizer.py`、`tests/agent/runtime/test_summarizer.py`

- [x] **Step 1:** `_summarize_json` 中：data 为 dict 且含文本字段（复用 `_TEXT_FIELD_PRIORITY` 提取）时，跑 `parse_sections`；节数 ≥2 → summary 末尾附：

  ```
  文档大纲: 一、项目背景(约4.5k字); 二、建设方案(约6.2k字); …（共 8 节，可用 get_artifact 的 section 参数按节读取）
  ```

  最多 10 个标题、500 字符封顶；无结构（单块/纯文本）不附加。
- [x] **Step 2:** 与 key_excerpts 采样器零耦合：大纲只进 summary 字符串，不动摘录选择与预算。
- [x] **Step 3:** 测试：长 markdown dict 的 summary 含大纲行且含"section 参数"提示；短文本/非 markdown dict 的 summary 不变；超长文档大纲截断到 10 个标题。

### Task 1.2: 行为指引

**Files:** `courtier/prompts/defaults/zh-CN/behavioral.yaml`、`en-US/behavioral.yaml`、`tests/courtier/test_prompt_single_source.py`

- [x] **Step 1:** `$ref` 规则区补一条（双语镜像）："读取长文档/长结果时，先用 get_artifact 的 outline=true 获取大纲，再用 section 参数按节读取，避免全文回读或盲翻页"。
- [x] **Step 2:** 防漂移测试增关键词断言（"outline"）。

---

## 提交切分

```
feat(artifacts): markdown/official-document outline parser               # 0.1
feat(tools): get_artifact outline/section structured reading             # 0.2
feat(summarizer): surface document outline in markdown result summary    # 1.1
docs(prompts): read long documents via outline then section              # 1.2
```

## 总验收

1. `uv run pytest -m "not integration"` 全绿。
2. 场景回放：长文档（>10k 字符 markdown）持久化后——summary 自带大纲行；`outline=true` 返回完整节列表；`section` 定点取节内容正确（含子节、超长截断标注）；无参数时行为与现状完全一致。
3. 上一轮修复回归：`$ref` 文本参数直传、registry-first 直达、inline id 读取均不受影响（既有测试覆盖）。

## 明确不做的事

- 不新增独立工具（document_outline/read_document_section）——能力内聚在 get_artifact。
- 不改 convert_document/parse_document 插件与持久化格式；大纲是读取时纯计算。
- 不动资源库坐标系（read_chunks 的 rid/chunk_no）；outline/section 只管 `$ref` 内容。
- 不做语义级切分（embedding 切块/主题分段）——结构解析只认标题与公文层级标记。
- 摘要大纲不进 key_excerpts（只进 summary 字符串），不调整摘录采样器预算。

---

## 实施记录（2026-08-16）

全部 4 个 Task 完成并逐项提交。实施中的偏差：

1. **Task 0.1 解析器两次修复**：(a) 第X章/节最初只匹配中文数字，`第3节`
   选择器失败——字符类扩展为 [一二三四五六七八九十百0-9]+；(b) `（一）`
   模式在一次脚本化替换中丢掉了 `rf` 前缀导致全角括号分支失效，已恢复。
2. **Task 0.2 结构化读取走全量 load()**：outline 需要整篇文本，而持久化
   read 路径按 max_tokens 截断——`_structured_read` 用 registry-first +
   `artifact_store.load()`（同步、磁盘优先含 ES 回退）取全文，再在内存中
   解析/切片。投影与分页路径完全绕过，无 outline/section 时行为不变
   （回归断言逐字节一致）。
3. **Task 1.1 大纲门槛**：低于 800 字符不附大纲（不值得提示导航），
   无标题退化"块 N"也不附（无信息量）；节标题带约数字符数，帮助模型
   决定读哪节。
4. **回归**：后端 1571 passed / 0 failed；lint 全绿。
