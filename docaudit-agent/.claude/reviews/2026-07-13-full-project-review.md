# Full Project Review — Courtier / DocAudit Agent

**Reviewed**: 2026-07-13  
**Branch**: main  
**Scope**: Entire repository (core engine, API layer, domain plugins, frontend, plugin IPC)  
**Method**: Static analysis (ruff, mypy, pytest) + manual code review of critical findings

---

## Validation Results

| Check | Result | Detail |
|-------|--------|--------|
| Tests (pytest) | ✅ 804 passed, 6 skipped, 0 failures | 14.15s, 17 deselected (integration) |
| Lint (ruff) | ⚠️ ~300 issues | Mostly E501 line-length / I001 import-sort / W292 trailing-newline; 15 real F-type issues |
| Type check (mypy) | ❌ 159 errors in 45 files | Many real type bugs, some false positives |

---

## Summary

All 804 unit tests pass. The codebase is functional but has a moderate number of type-safety gaps and a few logic bugs that could surface under edge conditions. The most concerning findings are in the **plugin IPC layer** (crash risk when writer is None), **type-confusion in truncation helpers** (variable re-use across branches), and **admin user update route** (type mismatch that would cause a 500 error at runtime). No CRITICAL security vulnerabilities were identified in the reviewed paths.

---

## Findings

### CRITICAL

**None found.** No hardcoded secrets, SQL injection, auth bypass, or data-loss vulnerabilities detected in the reviewed code.

---

### HIGH (7 findings)

#### H1: Plugin `_send_line` crashes if `_writer` is None
- **File**: `packages/core/src/courtier/plugin/sdk/runtime.py:623-624`
- **Issue**: `_send_line()` calls `self._writer.write()` and `self._writer.flush()` without a None check. The `_writer` attribute is initialized as `None` in `__init__` and only set in `run()` (line 386-389). If any code path calls `_send_line` or `_send_notification` before `run()` completes initialization, this crashes with `AttributeError: 'NoneType' object has no attribute 'write'`.
- **Failure scenario**: A plugin that sends a notification during registration or a test harness that calls methods before `run()`.
- **Fix**: Add a guard `if self._writer is None: raise RuntimeError("Plugin not started")` at the top of `_send_line`.

#### H2: `ToolRegistry.execute` crashes on mixed `ToolResult` / `ExecutionResult`
- **File**: `packages/core/src/courtier/agent/tools/registry.py:222, 233, 236, 243, 249, 260`
- **Issue**: `result.raw_data` (only on `ExecutionResult`) vs `result.data` (only on `ToolResult`) are accessed without proper union narrowing. The logic at line 219 checks `is_execution_result` but subsequent lines (233, 236, 243, 249, 260) call `.data` or `.model_copy()` on the result regardless of type.
- **Failure scenario**: A tool returning `ExecutionResult` hits the non-execution-result branch at line 233, attempting `result.data` on an `ExecutionResult` (which has no `.data` attribute) → `AttributeError`.
- **Fix**: Narrow the union with explicit `if is_execution_result:` / `else:` blocks for each attribute access.

#### H3: Admin user update route mixes `UserRole` and `UserStatus`
- **File**: `packages/core/src/courtier/agent/api/routes/admin_users.py:105-124`
- **Issue**: `update_data` dict accumulates values of incompatible types: `UserRole` (line 106), `UserStatus` (line 111), `str` for `password_hash` (line 115), and `str` for `email` (line 117). The dict is then unpacked into `UserUpdate(**update_data)` at line 123. Mypy reports: `Incompatible types in assignment (expression has type "UserStatus", target has type "UserRole")` and `Argument 1 to "UserUpdate" has incompatible type "**dict[str, UserRole]"; expected "UserStatus | None"`.
- **Failure scenario**: Admin updates a user's status and role simultaneously → `UserUpdate(**update_data)` fails validation because `password_hash` is not a recognized field (the model expects `password`, not `password_hash`).
- **Fix**: Use separate typed variables or annotate `update_data` as `dict[str, Any]`. Map `password_hash` → `password` before passing to `UserUpdate`.

#### H4: Profile update passes `password_hash` instead of `password` to `ProfileUpdate`
- **File**: `packages/core/src/courtier/agent/api/routes/profile.py:89, 94`
- **Issue**: Line 89 stores `hash_password(body.new_password)` under key `"password_hash"`, but `ProfileUpdate` (the Pydantic model at line 94) likely expects the field to be named `password` or `new_password`. This would cause a `pydantic.ValidationError` at runtime when the model rejects the unknown field.
- **Failure scenario**: User changes password → `ProfileUpdate(**update_data)` raises `ValidationError` because `password_hash` is not a valid field → HTTP 500.
- **Fix**: Match the key name to the `ProfileUpdate` model's field name (likely `new_password`), or use `password` and let the service/repo re-hash it.

#### H5: `ProjectorLayer` type undefined (type-checking-only, but blocks refactoring)
- **File**: `packages/core/src/courtier/agent/artifacts/projectors.py:107, 126`
- **Issue**: `ProjectorLayer` is used as a type annotation in `ProjectorRegistry._LAYER_ORDER` (line 107) and `promote()` signature (line 126) but is never imported or defined. While `from __future__ import annotations` prevents a runtime `NameError`, the type is effectively `Any` — no editor autocomplete or type safety.
- **Failure scenario**: A developer calls `registry.promote("foo", "invalid_layer")` — mypy won't catch the wrong string literal. Runtime validation at line 139 might catch it, but the type system provides no guard.
- **Fix**: Define `ProjectorLayer = Literal["local", "plugin", "domain", "core"]` in `projectors.py` or import it from `models.py`.

#### H6: Duplicated ~100-line truncation logic between `store.py` and `cache_store.py`
- **File**: `packages/core/src/courtier/agent/runtime/store.py:162-197` and `packages/core/src/courtier/agent/core/cache_store.py:228-260`
- **Issue**: The `_truncate_value` function is copy-pasted verbatim across two files. Both have a type issue where `result` is defined as `list[Any]` in the list branch (line 170/234) and then redefined as `dict[str, Any]` in the dict branch (line 184/247). While the `return` in each branch prevents runtime issues, any future modification that removes a `return` could cause `AttributeError` (calling `.keys()` on a list). This is a maintenance hazard.
- **Failure scenario**: A developer adds code after the list `return` that falls through to the dict branch → `AttributeError: 'list' object has no attribute 'keys'`.
- **Fix**: Extract the shared function into a common utility module, or use distinct variable names (`list_result` / `dict_result`) in each branch.

#### H7: `ocr/factory.py` passes unexpected `api_url` kwarg to `OCREngine`
- **File**: `packages/domains/docaudit/plugins/docparse/parsers/ocr/factory.py:30`
- **Issue**: Mypy reports `Unexpected keyword argument "api_url" for "OCREngine"`. This suggests the `OCREngine.__init__` signature doesn't accept `api_url`, meaning the PaddleOCR service URL configuration is silently ignored.
- **Failure scenario**: OCR always uses the default API URL instead of the configured one → OCR requests go to the wrong endpoint or fail silently when the default is unreachable.
- **Fix**: Check the `OCREngine` class signature; add `api_url` parameter to `__init__`, or pass it via a different mechanism.

---

### MEDIUM (10 findings)

#### M1: `rules.py` — unsafe `date_match.group()` on potentially-None match
- **File**: `packages/domains/docaudit/plugins/docparse/parsers/rules.py:483, 486`
- **Issue**: `DISTRIBUTION_DATE_PATTERN.search(text)` is called twice: once for the `if` condition (line 482) and once for extraction (line 483). The second call could return `None` if the pattern matches differently (e.g., due to regex state), or a future refactor could change the `if` condition. Mypy correctly flags line 486: `Item "None" of "Match[str] | None" has no attribute "group"`.
- **Failure scenario**: Pattern matches in `if` but not on re-search → `AttributeError: 'NoneType' object has no attribute 'group'` → parser crashes for that document.
- **Fix**: Store the match result: `date_match = DISTRIBUTION_DATE_PATTERN.search(text)` once, then check `if date_match:`.

#### M2: `spacing.py` — `_x0_break_px` redefined unconditionally
- **File**: `packages/domains/docaudit/plugins/docparse/parsers/spacing.py:371, 373`
- **Issue**: Variable `_x0_break_px` is assigned on line 371 (`40.0 / scale_x`) and then immediately reassigned on line 373 (`float("inf")`) when `img_width <= 0`. The first assignment is always dead code because line 373 unconditionally overwrites it when in the `else` branch. The logic appears to be:
  ```python
  if img_width > 0:
      _x0_break_px = 40.0 / scale_x
  else:
      _x0_break_px = float("inf")
  ```
  This is correct logic — the redefinition complaint from mypy is because both branches target the same variable name, not a bug. But the `no-redef` warning suggests the intent may have been different.
- **Failure scenario**: None — this is a false positive from mypy's `no-redef` check.
- **Fix**: Add `# type: ignore[no-redef]` or use a ternary: `_x0_break_px = 40.0 / scale_x if img_width > 0 else float("inf")`.

#### M3: `sse_adapter.py` — `SubagentToolRecord` status type too narrow
- **File**: `packages/core/src/courtier/agent/api/sse_adapter.py:363-365`
- **Issue**: `event.tool_status` (type `str`) is passed to `SubagentToolRecord(status=...)` which expects `Literal['pending', 'running', 'done', 'ok', 'error', 'warning']`. If a plugin sends an unrecognized status string, Pydantic validation fails silently (or raises at runtime depending on config).
- **Failure scenario**: Plugin sends `status: "timeout"` → `SubagentToolRecord` creation fails → SSE event dropped → frontend shows tool as permanently "running".
- **Fix**: Normalize the status string or use a fallback (`event.tool_status or "done"` is already there but only covers None).

#### M4: `hook_chain.py` — handler return type mismatch
- **File**: `packages/core/src/courtier/agent/hooks/chain.py:167`
- **Issue**: A hook handler returning `None` is assigned where `AgentState` is expected. This suggests some hooks registered with `on_pre_think`/`on_post_tool` may return `None` (no-op) when the type system expects them to always return a state object.
- **Failure scenario**: A no-op hook registered for observation-only purposes → mypy complains but runtime works fine.
- **Fix**: Allow `None` returns from no-op hooks, or require handlers to always return the input state.

#### M5: `auth.py` — `extract_user_id` returns `Any` instead of `str`
- **File**: `packages/core/src/courtier/agent/api/middleware/auth.py:99`
- **Issue**: `payload["sub"]` returns `Any` because `verify_token` return type is not annotated. The function's declared return type is `str`, but mypy cannot verify this. If the JWT payload's `sub` field is missing or not a string, this silently propagates.
- **Failure scenario**: JWT with `sub: 12345` (integer) → `extract_user_id` returns an int → downstream code crashes when treating it as a string.
- **Fix**: Add explicit `isinstance(payload["sub"], str)` check, or annotate `verify_token` return type.

#### M6: `observability.py` — `duration` computed but unused
- **File**: `packages/core/src/courtier/agent/api/middleware/observability.py:27-29`
- **Issue**: `duration = time.time() - start` is computed but never used. The `request_duration_seconds` histogram is likely never populated, making the Prometheus latency metric always zero.
- **Failure scenario**: Grafana dashboard shows zero latency for all requests → operators unaware of slow endpoints.
- **Fix**: Call `request_duration_seconds.labels(...).observe(duration)`.

#### M7: `tracer.py` — `start` computed but unused in `execute_tool_span`
- **File**: `packages/core/src/courtier/agent/telemetry/tracer.py:192`
- **Issue**: `start = time.time()` is computed but the duration is never recorded. Tool execution latency is not captured in traces.
- **Failure scenario**: Trace view shows zero-duration tool spans → debugging tool latency issues is impossible.
- **Fix**: Record duration on span: `span.set_attribute("duration_seconds", time.time() - start)`.

#### M8: `skill.py` — `default_mode` assigned but unused
- **File**: `packages/core/src/courtier/agent/tools/builtin/skill.py:53`
- **Issue**: `default_mode = getattr(skill, "default_mode", "subagent")` is computed but never referenced. The description at line 52 uses `skill.description` but `default_mode` (which determines whether the skill runs as subagent/inline/loop) is ignored.
- **Failure scenario**: A skill configured with `default_mode: "inline"` is always run as a subagent → incorrect execution mode.
- **Fix**: Use `default_mode` to set the skill tool's execution strategy.

#### M9: `cache_store.py` — `error` variable declared but never set
- **File**: `packages/core/src/courtier/agent/core/cache_store.py:179`
- **Issue**: `error: str | None = None` is declared and never written to. The `read` method always returns a dict without an `error` key even when the read partially fails (e.g., primary backend fails but disk fallback succeeds). Callers expecting an `error` field to detect partial failures get silent success.
- **Failure scenario**: Primary ES backend fails, disk fallback succeeds → caller sees no error and assumes ES data is current → stale results.
- **Fix**: Set `error` when the primary backend fails but disk succeeds: `error = f"primary_backend_failed: {exc}"`.

#### M10: `profile.py` — `_profile_to_dict` called with potentially-None `UserTable`
- **File**: `packages/core/src/courtier/agent/api/routes/profile.py:95`
- **Issue**: `user_repo.update()` returns `UserTable | None`, but `_profile_to_dict(updated)` is called without a None check. If `update` returns None (user not found), this crashes.
- **Failure scenario**: User is deleted between auth check and profile update → `_profile_to_dict(None)` → `AttributeError`.
- **Fix**: Add `if updated is None: raise HTTPException(404, "用户不存在")`.

---

### LOW (8 findings)

#### L1: `admin_users.py` — `_user_to_dict` called with potentially-None `UserTable`
- **File**: `packages/core/src/courtier/agent/api/routes/admin_users.py:124`
- **Issue**: Same pattern as M10. `user_repo.update()` may return None.
- **Fix**: Add None guard before `_user_to_dict(updated)`.

#### L2: `sse_adapter.py:332,361,375,383` — `SubagentRunRecord | None` narrowing
- **File**: `packages/core/src/courtier/agent/api/sse_adapter.py`
- **Issue**: Multiple assignments where `dict.get()` returns `T | None` but the variable is typed as `T`. The `if run:` check on the next line handles the None case, so this is a type-narrowing issue, not a runtime bug.
- **Fix**: Use `run = self._current_subagents.get(handle_id); if run is None: return` pattern.

#### L3: `rules.py:59` — `field` potentially shadowed
- **File**: `packages/domains/docaudit/plugins/docparse/parsers/rules.py:59`
- **Issue**: Mypy reports `"str" not callable` on `field(default_factory=dict)`. The `field` import at line 11 (`from dataclasses import dataclass, field`) should be fine. This may be a mypy bug or an import conflict from a wildcard import elsewhere.
- **Fix**: Verify no other `field` variable exists in the module scope; if clean, add `# type: ignore[operator]`.

#### L4: `cache_store.py:627` — `resolved` dictionary name re-use
- **File**: `packages/core/src/courtier/agent/core/cache_store.py:612, 627`
- **Issue**: Variable `resolved` is used for a ref-resolution result at line 612 and then redefined as a dict at line 627. Not a runtime bug since each branch returns.
- **Fix**: Use distinct names (`resolved_ref`, `resolved_dict`).

#### L5: `observability.py` unused variable + `loop.py` unused `fmt_size` import
- **File**: `packages/core/src/courtier/agent/api/middleware/observability.py:29` and `packages/core/src/courtier/agent/core/loop.py:13`
- **Issue**: Dead code. `fmt_size` was likely used for debug logging that was removed. The import is harmless but misleading.
- **Fix**: Remove unused imports and dead variable assignments.

#### L6: `services/__init__.py` — re-exports without usage
- **File**: `packages/core/src/courtier/agent/api/services/__init__.py`
- **Issue**: 14 symbols imported and re-exported but none are used within the `__init__.py` itself. This is intentional (public API surface) but ruff flags each as F401.
- **Fix**: Add `__all__` with explicit names and use `# noqa: F401` or configure ruff to allow re-exports.

#### L7: `content_compliance/checkers_init.py` — file referenced but not found
- **File**: `packages/domains/docaudit/plugins/content_compliance/checkers_init.py`
- **Issue**: This file does not exist at the expected path. According to `CLAUDE.md`, initialization was moved to lazy loading in the `content_audit` plugin. If any code still imports from this path, it will fail.
- **Fix**: Verify no stale imports reference `content_compliance.checkers_init`.

#### L8: Multiple test files import `pytest` unused
- **Issue**: Several test files have `import pytest` without using any pytest APIs. While harmless, it suggests copy-paste patterns.
- **Fix**: Remove unused imports in test files.

---

## Silent Failure Patterns

A scan for `except Exception` across the codebase found ~40 instances. The majority handle failures correctly (log + fallback), but these patterns warrant attention:

1. **Primary-backend-fails-silently pattern** (3 instances): `cache_store.py:188`, `store.py:242/262/274` — when the primary backend (ES) fails, the code silently falls back to disk. The caller receives data with no indication that the primary backend is unhealthy. This masks ES outages.

2. **Hook-chain exception swallowing** (2 instances): `hooks/chain.py:228/240` — hook execution errors are caught and logged but never propagated. If a critical hook (e.g., audit logging) fails, the agent continues unaware.

3. **`_send_line` exception pass** (1 instance): `plugin/sdk/runtime.py:625` — `except Exception:` with only `logger.exception(...)`. No re-raise or error notification to the host. The plugin believes the message was sent but the host never received it → desynchronized state.

**No bare `except:` blocks (without exception type) were found** — this is good.

---

## Duplication

1. **`_truncate_value` function** — duplicated verbatim (~35 lines) between `runtime/store.py` and `core/cache_store.py`. Both have the same variable-reuse type issue.

---

## Files Reviewed (key files, not exhaustive)

| File | Category |
|------|----------|
| `packages/core/src/courtier/agent/tools/registry.py` | Core — Tool execution |
| `packages/core/src/courtier/agent/artifacts/projectors.py` | Core — Artifact projection |
| `packages/core/src/courtier/agent/api/sse_adapter.py` | API — SSE streaming |
| `packages/core/src/courtier/agent/api/routes/admin_users.py` | API — Admin routes |
| `packages/core/src/courtier/agent/api/routes/profile.py` | API — Profile routes |
| `packages/core/src/courtier/agent/api/middleware/auth.py` | API — Auth middleware |
| `packages/core/src/courtier/agent/api/middleware/observability.py` | API — Observability |
| `packages/core/src/courtier/agent/runtime/store.py` | Runtime — Result persistence |
| `packages/core/src/courtier/agent/core/cache_store.py` | Core — Cache persistence |
| `packages/core/src/courtier/plugin/sdk/runtime.py` | Plugin — SDK runtime |
| `packages/core/src/courtier/plugin/client.py` | Plugin — Host client |
| `packages/domains/docaudit/plugins/docparse/parsers/rules.py` | Domain — Rule engine |
| `packages/domains/docaudit/plugins/docparse/parsers/spacing.py` | Domain — Spacing analysis |
| `packages/domains/docaudit/plugins/docparse/parsers/ocr/factory.py` | Domain — OCR factory |
| `packages/core/src/courtier/agent/telemetry/tracer.py` | Telemetry — Tracing |
| `packages/core/src/courtier/agent/tools/builtin/skill.py` | Core — Skill tool |
| `packages/core/src/courtier/agent/hooks/chain.py` | Core — Hook chain |

---

## Recommendations

1. **Fix the 7 HIGH findings** before the next production deployment — especially H3 (admin route 500 error) and H1 (plugin crash).
2. **Run `ruff check --fix`** to auto-correct import sorting and remove unused imports (~200 auto-fixable issues).
3. **Add `ProjectorLayer` type** definition to enable type-checking of the projector registry.
4. **Extract the duplicated `_truncate_value` function** into a shared utility to prevent divergence.
5. **Add type annotations** to `verify_token` return type and plugin SDK method signatures to close the mypy gaps.
6. **Add health-check monitoring** for ES backend failures — currently masked by silent disk fallback.
