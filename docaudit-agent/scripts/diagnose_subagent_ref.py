"""诊断脚本：追踪 $ref 在子代理 dispatch 链路中的解析状态。

对比两种场景：
  Scenario A: 同一轮 session 内调用 parse_document → ref 在 CacheStore 中 → 应能解析
  Scenario B: 多轮 session（模拟），ArtifactStore 有 artifact 但 CacheStore.ref_map 为空

用法: PYTHONPATH=. uv run python scripts/diagnose_subagent_ref.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent.agents.input_models import FormatAuditorInput
from agent.agents.subagent import _SubAgentTool as _SAT
from agent.core.cache_store import CacheStore
from agent.prompts.pipeline import PromptPipeline


def sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


MOCK_DOC = {
    "user_id": "",
    "doc_id": "a4e8d16cd25cc146f7f1eec559131cd1535f25b72035a77afd8c8655107b8ff8",
    "total_page_num": 1,
    "pages": [
        {
            "page_content": {
                "header": {
                    "copy_number": None,
                    "classification_duration": None,
                    "urgency_level": None,
                    "issuing_logo": {
                        "first_indent": 0.0, "left_indent": 0.0, "right_indent": 0.0,
                        "outline_level": 0, "alignment": "center",
                        "font_size": 22.0, "font_name": "方正小标宋简体",
                        "bold": True, "text": "XX市人民政府办公厅文件",
                    },
                    "issuing_number": {
                        "first_indent": 0.0, "left_indent": 0.0, "right_indent": 0.0,
                        "outline_level": 0, "alignment": "center",
                        "font_size": 16.0, "font_name": "仿宋_GB2312",
                        "bold": False, "text": "X政发〔2026〕1号",
                    },
                },
                "body": {
                    "title": "关于开展2026年度全市安全生产大检查工作的通知",
                    "paragraphs": [
                        "为深入贯彻落实习近平总书记关于安全生产的重要指示批示精神，切实加强全市安全生产工作。",
                        "一、工作目标",
                        "根据《安全生产法》精神，全面排查治理各类安全隐患。",
                        "二、检查范围",
                        "本次检查覆盖全市所有行业领域，重点检查危险化学品、矿山、建筑施工、交通运输等高危行业领域。",
                    ],
                },
            }
        }
    ],
}


def build_param_props() -> dict:
    """Build the parameter schema for FormatAuditorInput (same as _SubAgentTool)."""
    schema = FormatAuditorInput.model_json_schema()
    properties = {}
    for field_name, field_info in schema.get("properties", {}).items():
        prop = dict(field_info)
        if _SAT._is_union_with_dict(prop):
            prop = _SAT._inject_ref_hint(prop)
        properties[field_name] = prop
    return properties


def trace_ref_resolution(cache_store: CacheStore, scenario: str) -> None:
    sep(f"Scenario: {scenario}")

    param_props = build_param_props()
    ref_id = "$ref:parse_document:1"

    # Step 1: Check if ref is in CacheStore
    print(f"  1. CacheStore.ref_map 有 '{ref_id}'? "
          f"{'✅ YES' if ref_id in cache_store.ref_map else '❌ NO'}")

    # Step 2: Try resolve_refs (what ToolRegistry does)
    kwargs_before = {"document": ref_id, "doc_type": "通知", "task": "对文档进行格式审计"}
    kwargs_after = cache_store.resolve_refs(kwargs_before, param_props)
    doc = kwargs_after["document"]
    resolved = isinstance(doc, dict)
    print(f"  2. resolve_refs 结果: document = "
          f"{'dict ✅' if resolved else f'str({repr(doc)[:80]}) ❌'}")

    # Step 3: _SubAgentTool.execute() — cache_store not in kwargs
    print(f"  3. _SubAgentTool.execute(): cache_store from kwargs.pop = None "
          f"(registry 不传递 cache_store)")

    # Step 4: FormatAuditorInput creation
    execute_kwargs = dict(kwargs_after)
    try:
        input_obj = FormatAuditorInput(**execute_kwargs)
        doc_type_in_obj = type(input_obj.document).__name__
        print(f"  4. FormatAuditorInput.document type = {doc_type_in_obj}")
    except Exception as exc:
        print(f"  4. ❌ FormatAuditorInput creation failed: {exc}")
        return

    # Step 5: _resolve_input_refs (skipped because cache_store=None)
    print(f"  5. _resolve_input_refs: SKIPPED (cache_store is None)")

    # Step 6: base.py:run() context injection
    input_context: dict[str, str] = {}
    for field_name, field_value in input_obj.model_dump().items():
        if field_value is not None and field_name not in ("task", "explicit_inputs"):
            if isinstance(field_value, str):
                input_context[field_name] = field_value
            else:
                input_context[field_name] = json.dumps(
                    field_value, ensure_ascii=False, default=str
                )

    runner_context = {"file_path": "uploads/test.docx", "audit_base_dir": ".agent_logs"}
    merged = dict(runner_context)
    merged.update(input_context)

    doc_in_merged = merged.get("document", "")
    print(f"  6. merged context: document = "
          f"{repr(doc_in_merged[:80])}... ({len(doc_in_merged)} chars)"
          if len(doc_in_merged) > 80 else
          f"  6. merged context: document = {repr(doc_in_merged)}")

    # Step 7: PromptPipeline truncation
    pipeline = PromptPipeline()
    pipeline._identity = "# 身份\n你是 FormatAuditorAgent。"
    system_prompt = pipeline.build(merged)
    if "任务上下文" in system_prompt:
        ctx_lines = [l for l in system_prompt.split("\n") if l.startswith("- document")]
        if ctx_lines:
            display_line = ctx_lines[0]
            if len(display_line) > 100:
                print(f"  7. System prompt: {display_line[:100]}... (truncated)")
            else:
                print(f"  7. System prompt: {display_line}")

    # Step 8: Subagent's own ref resolution for audit_format
    print(f"\n  8. 子代理内部调用 audit_format(document='{ref_id}'):")
    # Subagent's loop.py gets cache_store from context_manager._cache
    # which is the SAME CacheStore — still no ref_map entry
    sub_kwargs = {"document": ref_id}
    audit_param_props = {
        "document": {"anyOf": [{"type": "object"}, {"type": "string"}]},
    }
    sub_resolved = cache_store.resolve_refs(sub_kwargs, audit_param_props)
    sub_doc = sub_resolved["document"]
    sub_ok = isinstance(sub_doc, dict)
    print(f"     子代理 cache_store.resolve_refs → "
          f"{'dict ✅' if sub_ok else f'str({repr(sub_doc)[:80]}) ❌'}")
    if not sub_ok:
        print(f"     → audit_format.execute() 将报错: "
              f"'document 参数必须是 dict 或有效的 JSON 字符串，收到 str'")


def main() -> None:
    cache_dir = tempfile.mkdtemp(prefix="diag_cache_")

    # ── Scenario A: 同一轮 session，parse_document 已被调用 ──
    cache_a = CacheStore(cache_dir=cache_dir)
    cache_a.persist(MOCK_DOC, "parse_document", force=True)
    trace_ref_resolution(cache_a, "A: 同一轮 session (CacheStore 有 ref)")

    # ── Scenario B: 多轮 session，新 CacheStore 但旧缓存文件 ──
    # 模拟：新请求创建新的 CacheStore（ref_map 为空），
    # 但磁盘上还有 parse_document 的缓存文件（由 _rehydrate_artifact_store 加载到 ArtifactStore）
    cache_b = CacheStore(cache_dir=cache_dir)  # ref_map 为空！
    trace_ref_resolution(cache_b, "B: 多轮 session (CacheStore.ref_map 为空，文件在磁盘)")

    # ── 结论 ──
    sep("结论")
    print("""
  Scenario A 中，CacheStore.ref_map 有 $ref:parse_document:1 → resolve_refs 成功 → dict
  Scenario B 中，CacheStore.ref_map 为空（新实例）→ resolve_refs 失败 → 仍是 str

  这就是日志中看到的 bug：
  - 多轮 HTTP 请求每次创建新的 CacheStore（ref_map 为空）
  - ArtifactStore 被 _rehydrate_artifact_store 重建（有 artifact）
  - 但 CacheStore.ref_map 没有被同步重建
  - 导致 registry.resolve_refs() 找不到 ref → 子代理收到 $ref 字符串 → audit_format 报错
    """)

    import shutil
    shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
