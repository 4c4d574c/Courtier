# docaudit-agent Full Backend Review

**Reviewed:** 2026-07-02  
**Scope:** Entire `docaudit-agent` backend (`src/`, `plugins/`, `tests/`, `alembic/`, config).  
**Decision:** **BLOCK** — multiple CRITICAL security and correctness issues must be fixed before merge/deploy.

## Executive Summary

The backend is functionally rich and has good unit-test coverage in isolated suites, but it is not production-ready as-is. The most serious problems are:

1. **Security:** `.env` files containing real credentials are tracked by git; plugin tools can read arbitrary host files; authentication routes lack ownership checks and rate limiting.
2. **Correctness:** The result store can corrupt dict/list return values, semantic-version compatibility is checked with tuple ordering, and context compaction can crash when no model is configured.
3. **Concurrency / Performance:** Heavy document parsing and Elasticsearch I/O run synchronously inside async code; the database layer has N+1 loading; large uploads and objects are fully buffered in memory; session persistence is O(n²).
4. **Code Quality:** ~151 files fail `black`, `ruff` reports 102 errors, and `mypy` reports 265 errors. The default `pytest` invocation also fails to collect due to `data/` permission errors and cross-directory import issues.

## Validation Results

| Check | Command | Result | Notes |
|---|---|---|---|
| Unit tests (agent) | `uv run pytest tests -m "not integration" -q --ignore=data` | **Pass** | 736 passed, 27 deselected, 41 warnings |
| Unit tests (domain) | `uv run pytest src -m "not integration" -q --ignore=data` | **Pass** | 470 passed, 1 skipped |
| Default pytest | `uv run pytest -m "not integration" -q` | **Fail** | Permission denied in `data/langfuse-ch/*`; `ModuleNotFoundError: No module named 'docparse.parsers'` when `tests/` and `src/` are collected together |
| Formatting | `uv run black --check src tests` | **Fail** | 151 files would be reformatted |
| Lint | `uv run ruff check src tests` | **Fail** | 102 errors (unused imports, E402, etc.) |
| Type check | `uv run mypy src` | **Fail** | 265 errors (many missing stubs; several real type errors noted below) |

## CRITICAL Findings

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 1 | `.env` | multiple | `.env` is tracked by git and contains real secrets (MySQL password, `CEC_API_KEY`, MinIO keys, Langfuse keys, Grafana password, JWT secret, admin password). A worktree `.env` also exists. | Rotate every exposed credential; remove `.env` and worktree `.env` from git history (`git filter-repo` / BFG); add `.env` to `.gitignore`; keep only `.env.example`. |
| 2 | `plugins/common/parse/tools.py` | 86–123 | `parse_document` accepts any `file_path`, resolves it, and reads the file. No sandbox root prevents reading `/etc/passwd`, host `.env`, source code, etc. | Restrict paths to a configured safe root (`settings.upload_dir`); validate `resolved.is_relative_to(root)`. |
| 3 | `plugins/common/annotate/tools.py` + `src/docannot/_annotate.py` | 59–64 / 27–29 | `annotate_document` treats `source` as a file path and reads it without validation, allowing arbitrary file reads. The documented base64 input path is also not implemented. | Validate paths against a safe root; detect and decode base64 inline input before passing to `annotate()`. |
| 4 | `src/plugin/sdk/runtime.py` | 930 | `_run_stdin_loop` logs raw JSON-RPC request lines at INFO, including `model_config` which typically contains the LLM `api_key`. | Log only method + request ID, or redact `api_key`, `authorization`, `password`, `token` before logging. |
| 5 | `src/agent/runtime/store.py` | 158–167 | `_truncate_data` returns `serialized[:chars]` for `dict`/`list` inputs, converting structured data into a partial JSON string. Callers expecting the original type break. | Return a parsed, truncated structure or a wrapper `{"_truncated": True, "data": ...}` preserving type. |
| 6 | `src/agent/artifacts/models.py` | 740–766 | `check_schema_version_compatible` compares version tuples lexicographically, so `(1,10) < (1,2)`. | Compare components numerically, pad to equal length, or use a semver library. |
| 7 | `src/agent/core/context_manager.py` | 293 | `_full_compact` calls `await self._model.generate(...)` without checking `self._model is not None`, raising `AttributeError`. | Guard with `if self._model is None:` and fall back to the existing keep-recent-messages path. |
| 8 | `src/agent/core/loop.py` + `src/agent/core/audit_logger.py` | 402–404 / 147–188 / 197–241 | `_maybe_write_audit_turn` invokes synchronous `write_audit_turn`, which calls `Path.write_text` directly, blocking the async event loop. | Make `write_turn`/`finalize` async and offload disk writes to `asyncio.to_thread`. |
| 9 | `src/docparse/parsers/scanned/__init__.py` + `pdf_parser.py` + `docx_parser.py` + `plugins/common/parse/tools.py` | 73 / 44 / 45 / 86 | Heavy CPU/I/O (PDF rendering, PIL, OCR, LLM) runs in synchronous `parse()` and is awaited directly, blocking the event loop. | Run `parse()` in `asyncio.to_thread`, or convert parsers to async and offload CPU stages to a bounded process/thread pool. |
| 10 | `src/dbop/load_doc.py` | 165–174 | `load_doc` loops pages and calls `load_page()` per page, causing N+1 queries for paragraphs/elements. | Eager-load with SQLAlchemy `selectinload`, or batch page IDs. |
| 11 | `src/agent/api/services/file_service.py` + `src/docparse/parsers/*.py` | 56 / 64 / 67 / 102 | Uploads and documents are fully read into memory (`file.read()`, `path.read_bytes()`). | Stream uploads to a temp file with a max-size counter; compute hashes incrementally; avoid retaining full byte buffers during rendering. |
| 12 | `src/es/client.py` | 76–168 | Elasticsearch client uses synchronous `elasticsearch-py` HTTP I/O and is called from async code. | Wrap calls in `asyncio.to_thread`, migrate callers to async `ElasticsearchResultBackend`, or use `AsyncElasticsearch`. |
| 13 | `src/agent/core/state.py` + `src/agent/core/context_manager.py` | 160–206 / 28 / 137–192 | Every tool result is appended to `AgentState.messages`; full compaction only triggers at 40k chars; `micro_compact` writes old results to disk every turn. | Lower compaction trigger, bound retained tool messages, and persist large results to `CacheStore` before adding to state; avoid re-persisting unchanged messages. |

## HIGH Findings

### Security / Auth

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 14 | `src/agent/api/routes/sessions.py` + `src/agent/api/routes/control.py` | 197–213 / 13–63 | Any authenticated user can read, delete, pause, resume, or stop any session. No ownership checks. | Associate sessions with JWT `sub`; enforce ownership in `get_session`, `delete_session`, and control routes. |
| 15 | `src/agent/api/routes/sessions.py` | 70–71 | `list_sessions` returns every session, not just the current user's. | Filter by authenticated user's subject. |
| 16 | `src/agent/api/routes/auth.py` | 29 | `/login` has no rate limiting. | Add `@limiter.limit("5/minute")` or stricter. |
| 17 | `src/agent/api/middleware/auth.py` | 67–85 | JWT is accepted from the `?token=` query parameter for SSE, exposing it to proxy logs and browser history. | Issue short-lived, single-use SSE tickets derived from the JWT. |
| 18 | `src/plugin/registry.py` | 151–169 | Plugin tools register with `force=True`, so a plugin can shadow host tools. | Use `force=False` by default; namespace plugin tools or require explicit opt-in for overwrites. |
| 19 | `src/plugin/manager.py` | 245–248 | Plugin manifest `entry` is appended to `plugin_dir` without validation; `../malicious.py` could escape the directory. | Reject path separators/`..`; verify resolved path is inside `plugin_dir`. |
| 20 | `src/plugin/manager.py` | 272–276 | `PYTHONPATH` includes the project root, letting plugins import host `src.*` modules. | Restrict plugin `PYTHONPATH` to the minimum required; consider separate environments. |
| 21 | `src/plugin/proxies.py` | 129–147 | `ProxyChecker.check` assumes the JSON-RPC response is a dict; malformed responses raise `AttributeError`. | Validate response shape and return a failed `ComplianceResult` on malformed input. |
| 22 | `src/plugin/manager.py` | 172–178 | `_handle_host_request` returns `str(exc)` to plugins, potentially leaking internal config/connection strings. | Log full exception server-side; return a generic "Host service failed" message. |
| 23 | `src/agent/api/middleware/observability.py` | 27–29 | `/metrics` is public; only a comment warns about network-level protection. | Require JWT or enforce access control at the reverse proxy. |
| 24 | `src/agent/api/services/file_service.py` | 46–54 | `allowed_mimes` includes `application/octet-stream` and does not verify file magic bytes. | Remove the blanket fallback; inspect magic bytes and reject mismatches. |

### Concurrency / Performance

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 25 | `src/agent/api/app.py` + `src/agent/api/routes/control.py` | 117 / 14–26 | A single `asyncio.Event` on `app.state.pause_event` pauses/resumes all sessions globally. | Use a per-`session_id` dict of events. |
| 26 | `src/agent/api/services/stream_service.py` | 359–402 | `generate_sse_stream` starts `runner()` as a background task but never cancels it on client disconnect. | Cancel and await the task in the generator's `finally` block. |
| 27 | `src/agent/api/services/file_service.py` + `routes/files.py` | 31–78 / 10–18 | Entire upload buffered in RAM; `write_bytes`/`mkdir` are sync. | Stream to a temp file in chunks; offload sync disk I/O to threads. |
| 28 | `src/agent/api/services/agent_service.py` | 54–100 / 131–152 | `build_audit_agent`/`build_chat_agent` are async but perform blocking directory scanning and file reads; `build_chat_agent` has no awaits. | Make them sync and call via `asyncio.to_thread`, or convert blocking parts. |
| 29 | `src/agent/api/session_store.py` | 103–121 / 189–201 | Every mutation serializes the entire session to disk, making session persistence O(n²). | Use append-only JSONL or throttle persistence to step/session end. |
| 30 | `src/validator/content_checker.py` | 453–529 | `_process_chunk` reloads rules per chunk; `check_async` fans out all chunks with unbounded concurrency. | Load rules once per domain; bound concurrency with `asyncio.Semaphore`. |
| 31 | `src/agent/core/cache_store.py` | 197–201 | `recent_files` is truncated but the underlying disk files are never deleted. | Delete evicted files; add TTL/total-size cap. |
| 32 | `src/storage/minio_backend.py` | 39–44 | `_get_sync` returns full object bytes. | Return a streaming response or support chunked iterators. |
| 33 | `src/agent/runtime/store.py` | 183–195 | `active_backend` pins primary after first access without re-probing; failures persist until restart. | Probe primary on each access or reset to `None` after exceptions. |
| 34 | `src/agent/runtime/runtime.py` | 224 | `AgentRuntime.delegate` creates a `ContextManager(model=None)`, so compaction cannot summarize. | Pass the runtime model into the fallback context manager or require callers to provide one. |
| 35 | `src/docparse/parsers/pdf_parser.py` | 254–261 | Every PDF page is rendered to PNG bytes at 150 DPI and kept in memory. | Render lazily or store references/paths. |
| 36 | `src/agent/core/loop_phases.py` | 82–86 | Tool schemas are rebuilt and sent on every LLM call. | Cache filtered schemas per run and invalidate only on registry changes. |
| 37 | `src/agent/core/loop_utils.py` + `loop_guards.py` | 66–72 / 78–88 | `similarity()` uses `difflib.SequenceMatcher.ratio()`, which is O(n²) on long reasoning text. | Use a cheaper heuristic; fall back to difflib only for short strings. |
| 38 | `src/plugin/proxies.py` | 80–115 / 208–341 | Large tool/agent payloads are serialized over stdio JSON-RPC in memory. | Pass large payloads by reference (MinIO/cache path); enforce `_MAX_RESPONSE_SIZE`. |
| 39 | `src/docparse/parsers/registry.py` | 27–48 | `_is_scanned_pdf` opens every page and calls `get_text("text")`. | Sample a limited number of pages or decide lazily during parsing. |

### Correctness

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 40 | `src/docparse/parsers/page_regions.py` | 55–65 | Footer boundary is set to the first non-footer line after the footer block, misclassifying footer content. | Set boundary to the first footer-keyword line. |
| 41 | `src/dbop/load_doc.py` | 37 / 47 | `order_by="section_type, order_index"` and similar comma-separated strings are silently ignored by `CRUDRepository.list()`. | Split `order_by` on commas and build a composite SQLAlchemy clause. |
| 42 | `src/validator/format_checker.py` | 167 | Font sizes compared with exact float equality, causing false positives. | Compare with tolerance, e.g. `abs(a-b) > 0.5`. |
| 43 | `src/docparse/parsers/docx_parser.py` | 363 | `_extract_all_lines` ignores content inside tables. | Iterate `doc.tables` and yield cell text, or document limitation. |
| 44 | `src/docbuilder/builder.py` | 44–65 | Cross-page paragraph merge only merges two consecutive pages; a paragraph spanning ≥3 pages is not fully merged. | Keep the merged paragraph in the current page's last slot for further merging. |
| 45 | `src/agent/tools/protocol.py` | 37–59 | `ToolProtocol` requires abstract `summarize`, but `ListArtifactsTool`, `GetArtifactTool`, `PersistOutputTool`, `EchoTool` do not implement it. | Make `summarize` optional with a default, or add a base mixin. |
| 46 | `src/agent/permissions/gate.py` | 22–38 | `require_confirmation` stores a message but `allow()` always returns `True`; confirmation is not enforced. | Implement enforcement or rename API to clarify it is unenforced. |
| 47 | `src/agent/agents/base.py` | 132 | `Agent.__init__` silently falls back to `MockModelClient` when `model` is omitted. | Require an explicit model or raise `ValueError`. |
| 48 | `src/agent/core/cache_store.py` | 127–129 / 166–177 / 328–349 | `_next_ref_id` mutates counters outside the lock; sync file I/O inside async methods. | Acquire the lock around counter increments; offload reads/writes to threads. |
| 49 | `src/agent/core/loop.py` | 259–266 | If one parallel tool call is denied, the entire turn is aborted. | Execute allowed calls and report denials as tool results, or document all-or-nothing behavior. |
| 50 | `src/agent/core/state.py` | 165–169 | `add_observation` raises `ValueError` if result/tool-call counts mismatch; the loop does not catch it. | Return an `errored` state or handle the mismatch in the loop. |
| 51 | `src/plugin/sdk/runtime.py` | 1054–1071 | Coroutine handlers are dispatched in a fresh event-loop thread via `loop.run_in_executor`, which can deadlock with main-loop resources. | Await coroutine handlers in the main plugin loop; use thread pool only for sync handlers. |
| 52 | `src/plugin/sdk/runtime.py` | 674–687 | `run_agent()` retries on `except Exception`, swallowing `asyncio.CancelledError`. | Catch `CancelledError` separately and re-raise. |
| 53 | `src/doccorrector/corrector.py` | 426 | Hardcoded private-IP fallback for correction API. | Remove fallback; raise if no base URL is configured. |
| 54 | `src/agent/core/model.py` | 124–139 | `_fix_unescaped_ref_quotes` applies a fixed `offset += 2` that can shift later multi-ref positions incorrectly. | Remove or correct the offset to reflect actual length changes. |
| 55 | `src/agent/telemetry/decorators.py` | 25–26 | `traced_agent` pops `session_id` from kwargs and does not pass it through. | Pass `session_id` through or do not pop it. |

### Database / Persistence

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 56 | `src/agent/api/app.py` | 110–128 | No shared `AsyncDatabase` is created in the FastAPI lifespan; callers create their own engines. | Create one engine in lifespan, store on `app.state.db`, inject everywhere, and dispose on shutdown. |
| 57 | `src/dbop/db_manager.py` | 28–31 | Default engine settings; no pool size, recycle, or pre-ping. | Configure `pool_size`, `max_overflow`, `pool_recycle`, `pool_pre_ping`. |
| 58 | `alembic/` | — | `alembic/versions/` directory does not exist; no migrations exist. | Generate an initial migration; use Alembic for schema changes. |
| 59 | `alembic.ini` | 89 | `sqlalchemy.url` is a placeholder. | Read URL from env in `env.py`; keep placeholder in ini. |
| 60 | `src/dbop/tables/document.py` | 34–46 | No unique constraint on `(user_id, doc_id)`, allowing duplicates. | Add `UniqueConstraint("user_id", "doc_id")`. |
| 61 | `src/dbop/tables/page.py` / `paragraph.py` / `element.py` | 42 / 55 / 55–60 | Foreign keys lack explicit indexes. | Add indexes on `document_id`, `page_id`, `paragraph_id`. |
| 62 | `src/dbop/tables/rule.py` | 43–45 / 29 | `domain_id` / `rule_file_id` have no foreign keys. | Add `ForeignKey` constraints with `ON DELETE` behavior. |
| 63 | `src/dbop/tables/format_template.py` | 34–36 | Multiple rows can be `is_default=True` for the same `doc_type`. | Add a partial unique index on `(doc_type)` where `is_default=True`. |

### API / Observability

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 64 | `src/agent/api/services/stream_service.py` | 274 | `asyncio.Queue()` has no `maxsize`, allowing unbounded growth. | Set `maxsize=128` (or similar) for backpressure. |
| 65 | `src/agent/api/routes/control.py` + `stream_service.py` | 39–63 / 360–361 / 399–401 | `active_tasks` dict is mutated without synchronization. | Guard with `asyncio.Lock` or use a dedicated session manager. |
| 66 | `src/agent/api/session_store.py` | 260–268 / 270–280 | `SessionRecord` is frozen but `_load()` mutates `steps`/`thoughts` with `.append()`. | Build lists locally and create the dataclass via `replace(...)` or at the end. |
| 67 | `src/agent/api/sse_adapter.py` | 365–367 / 275–289 | `json.dumps` has no `default` handler; non-serializable payloads crash the stream. | Add `default=str` or sanitize payloads before emission. |
| 68 | `src/agent/api/routes/control.py` | 43–45 | `plugin_system.cancel_pending()` is called globally even when stopping one session. | Scope cancellation to the target session where possible. |
| 69 | `src/agent/api/routes/sessions.py` | 52–194 | `handle_sessions` is ~140 lines and mixes list/create/continue logic. | Split into separate handlers and a shared `_build_agent_context` helper. |
| 70 | `src/agent/api/services/stream_service.py` | 221–228 | Accesses private `context_manager._cache` and `_cache_dir`. | Add public accessors to `ContextManager`. |
| 71 | `src/agent/api/routes/sessions.py` / `routes/files.py` | 52 / 197 / 207 / 10 | Response models are not declared; plain dicts/dataclasses are returned. | Define Pydantic `response_model`s for documentation and validation. |
| 72 | `src/agent/api/app.py` | 118 | `app.state.active_tasks: dict[str, asyncio.Task] = {}` is invalid for mypy. | Use a dedicated state dataclass or `typing.cast`. |
| 73 | `src/agent/api/middleware/observability.py` | 27–29 | Latency is computed but not exposed; `time.time()` is not monotonic. | Use `time.perf_counter()` and expose a histogram metric. |

## MEDIUM Findings (selected)

| # | File | Lines | Issue | Recommended Fix |
|---|---|---|---|---|
| 74 | `src/dbop/db_manager.py` | 50–61 | `session()` always commits, even for read-only callers. | Provide a `commit=False` option or separate read-only context manager. |
| 75 | `src/dbop/save_doc.py` | 151–154 / 76 | Manual resource deletion and extra SELECT after paragraph bulk insert. | Use SQLAlchemy cascade or bulk delete; capture inserted IDs with `RETURNING`. |
| 76 | `src/dbop/db_manager.py` | 186–207 / 209–216 | `update`/`delete` fetch then mutate; two round-trips and race conditions. | Use DML with `returning()`. |
| 77 | `src/dbop/tables/*.py` | — | Pydantic `Create`/`Update` schemas live in ORM model files. | Move schemas to a `schemas/` package. |
| 78 | `src/dbop/tables/library.py` | 36 | Table name is `"librarys"` instead of `"libraries"`. | Rename before production data exists. |
| 79 | `src/dbop/tables/paragraph.py` | 56 | `section_type` uses MySQL `Enum`, requiring migrations for new values. | Use `String(64)` with app-level validation unless strict DB enum is required. |
| 80 | `src/dbop/tables/*.py` | — | Timestamp columns use `DateTime` without timezone. | Use `DateTime(timezone=True)` and store UTC consistently. |
| 81 | `src/dbop/tables/document.py` / `library.py` | 35 / 39 | `user_id`/`doc_id` are `String(512)` with no indexes. | Add indexes on frequently filtered columns. |
| 82 | `src/dbop/db_manager.py` | 76–115 / 157–166 | Unknown filter fields and invalid `order_by` strings are silently ignored. | Raise `ValueError` for unknown fields/order values. |
| 83 | `src/validator/format_checker.py` | 149–193 / 364–409 | `_check_element_font` nested function is dead code nearly identical to module-level `_check_element_font_module`. | Remove the nested dead code. |
| 84 | `src/docparse/parsers/rules.py` | 344 | Operator-precedence bug: `if "。" in text or "，" in text and len(text) > 40`. | Add parentheses to match intended logic. |
| 85 | `src/docparse/parsers/rules.py` | 482–486 | Regex search called twice; possible `None` dereference. | Save match result and reuse. |
| 86 | `src/docparse/parsers/spacing.py` | 371–373 | Same variable annotated in both `if`/`else` branches, causing mypy redefinition error. | Annotate once before the branch. |
| 87 | `src/doccorrector/corrector.py` | 336–381 | Protected-word check tests each error in isolation; combined edits may still destroy protected words. | Build fully corrected text first, then verify protected words. |
| 88 | `src/docparse/parsers/structure_recognizer.py` | 398–400 | Mutates Pydantic `Font.text` in place. | Use `model_copy(update={...})`. |
| 89 | `src/docannot/_patch.py` | 192–198 | Monkey-patches `docxnote` internals at import time; fragile on upgrade. | Pin version and add integration test. |
| 90 | `src/docbuilder/converter.py` | 41–45 | `_SHARED_PATTERNS` strips `^`/`$` and concatenates regexes, possibly weakening matching. | Test patterns individually or keep anchors. |
| 91 | `src/validator/templates/preview.py` | 148–214 | `_block_to_paragraph(block: dict)` is called with `dict | None`. | Update signature or guard calls. |
| 92 | `src/dbop/save_doc.py` | 55 | `Dict` referenced but not imported. | Use `dict[str, Any]`. |
| 93 | `src/docparse/parsers/ocr/factory.py` | 30 | Protocol instantiated with keyword argument causes mypy error. | Use concrete factory type or `Callable`. |
| 94 | `src/plagiarism/core.py` | 78–85 | `build_library_distribution` is O(N² × M²) and runs unconditionally. | Cache distribution or use MinHash pre-screening. |
| 95 | `src/agent/tools/builtin/list_artifacts.py` | 81–90 | Filters use substring matching, causing false positives. | Use exact equality or explicit allowed values. |
| 96 | `src/agent/artifacts/executor.py` | 144–145 | `_step_cache` grows monotonically with no eviction. | Add LRU eviction or clear between turns. |
| 97 | `src/agent/artifacts/projectors.py` | 101 / 120 | Uses `ProjectorLayer` without importing it. | Import `ProjectorLayer` from `.models`. |
| 98 | `src/agent/core/context_manager.py` | 149–152 | If `recent_tool_results == 0`, `tool_indices[:-0]` returns the whole list. | Treat `<= 0` as no-op. |
| 99 | `src/agent/core/structured_log_handler.py` | 55 | JSONL file opened in `__init__` but only closed on explicit `close()`. | Implement `__del__` or context manager and close handlers on reset. |
| 100 | `src/agent/api/services/stream_service.py` | 314 | `on_tool_progress` creates fire-and-forget tasks with unobserved exceptions. | Attach done callback to log exceptions or await directly. |
| 101 | `src/plugin/manager.py` | 507 / 532 / 547 | Health/stderr monitoring swallows exceptions without logging. | Log exception before returning False/calling `_on_crash`. |
| 102 | `src/plugin/client.py` | 119–122 | Malformed JSON-RPC lines are silently dropped. | Return a JSON-RPC `parse error` response and log the issue. |
| 103 | `src/plugin/protocol.py` | 27–34 | `JSONRPCResponse` validator rejects `result: null`. | Distinguish missing from explicit `null` with a sentinel. |
| 104 | `src/agent/api/routes/sessions.py` | 89 | `json.loads` on persisted session not wrapped; corrupt JSON yields 500. | Handle `JSONDecodeError` and return 400/404. |
| 105 | `src/agent/api/session_store.py` | 116–118 | `update()` silently ignores unknown kwargs. | Raise or log warning for unexpected keys. |
| 106 | `src/config.py` | 198 | `Settings.model_config` uses `extra: "ignore"`, hiding env var typos. | Consider `extra: "forbid"` in tests or log warnings. |

## LOW Findings (selected)

- ~151 files fail `black --check`.
- `ruff` reports 102 errors (unused imports, E402 in `tests/scripts/`, etc.).
- `mypy` reports 265 errors; many are missing third-party stubs, but real issues include `ToolProtocol` mismatches, incorrect `ModelClient | None` handling, and invalid `app.state` type annotations.
- Several `tests/scripts/*` use `sys.path.insert` and are marked integration but live under `tests/`; clean up import order.
- `tests/agent/test_cache_store.py`, `test_context_manager.py`: non-async tests marked with `@pytest.mark.asyncio` produce warnings.
- `src/validator/templates/preview.py`: missing default seal image silently produces blank seal.
- `src/agent/memory/store.py`: directory removal errors swallowed.
- `src/agent/api/sse_adapter.py`: missing tool metadata resolved with `except KeyError: pass`.

## Immediate Recommended Actions

1. **Rotate all secrets** exposed in `.env` and worktree `.env` files; purge them from git history.
2. **Fix arbitrary file reads** in `parse_document` and `annotate_document` by enforcing a safe path root.
3. **Stop logging sensitive JSON-RPC payloads** in plugin runtime.
4. **Add session ownership checks** and rate limiting before the backend is exposed to multiple users.
5. **Fix the three CRITICAL correctness bugs** in `runtime/store.py`, `artifacts/models.py`, and `context_manager.py`.
6. **Make document parsing, Elasticsearch, and audit logging async-safe** (offload to threads or use async clients).
7. **Eliminate DB N+1 loading** in `load_doc` and use a shared database engine.
8. **Generate the initial Alembic migration** and add missing indexes/constraints.
9. **Reformat with `black`**, fix `ruff` errors, and address the real `mypy` errors.
10. **Fix the default `pytest` collection** by ignoring `data/` and resolving the `docparse.parsers` import conflict when `tests/` and `src/` are collected together.
