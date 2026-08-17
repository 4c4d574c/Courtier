"""doccorrector 规则层与一致性单测。

对应实施计划 Phase 0/1（docs/superpowers/plans/2026-08-16-...）：
- Phase 0：引号开闭配对、数字内小数点豁免（纯函数直测）；
- Phase 1：errors 与 target 构造性一致、模型/规则重叠消解、
  _apply_errors 应用语义（mock 模型层，不发起网络）。
"""

from unittest.mock import MagicMock, patch

from doccorrector.corrector import (
    PROMPT_PREFIX,
    ErrorCorrect,
    OpenAITextCorrectInfer,
    _apply_errors,
    _detect_punctuation_mixing,
)


def _make_corrector(model_outputs: list[str]) -> ErrorCorrect:
    """构造不发网络的 ErrorCorrect，模型层直接返回给定输出。"""
    corrector = ErrorCorrect(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    # 实例属性遮蔽类方法，跳过 OpenAI 客户端推理
    corrector.inferencer.infer = lambda input_list: [  # type: ignore[method-assign]
        {"text": t, "warnings": []} for t in model_outputs
    ]
    return corrector


def _double_quotes(text: str) -> list[dict]:
    return [e for e in _detect_punctuation_mixing(text) if e["original"] == '"']


def _single_quotes(text: str) -> list[dict]:
    return [e for e in _detect_punctuation_mixing(text) if e["original"] == "'"]


def test_double_quote_pairing_alternates_open_close():
    text = '"加快建设数字中国"和"一网通办"'
    quotes = _double_quotes(text)
    assert [e["corrected"] for e in quotes] == ["“", "”", "“", "”"]
    # 位置必须落在原文每个半角引号处，且顺序一致
    assert [e["position"] for e in quotes] == [i for i, ch in enumerate(text) if ch == '"']


def test_single_quote_pairing_alternates_open_close():
    text = "'单引号'"
    quotes = _single_quotes(text)
    assert [e["corrected"] for e in quotes] == ["‘", "’"]


def test_quote_state_resets_between_calls():
    first = _double_quotes('"甲"')
    second = _double_quotes('"乙"')
    assert [e["corrected"] for e in first] == ["“", "”"]
    assert [e["corrected"] for e in second] == ["“", "”"]


def test_decimal_point_between_digits_exempt():
    text = "本项目总投资估算为人民币5.8亿元。"
    errors = _detect_punctuation_mixing(text)
    assert not any(e["original"] == "." for e in errors)


def test_multiple_decimals_all_exempt():
    text = "投资5.8亿元，申请3.5亿元。"
    assert _detect_punctuation_mixing(text) == []


def test_sentence_final_halfwidth_period_still_flagged():
    text = "会议结束."
    assert _detect_punctuation_mixing(text) == [
        {
            "original": ".",
            "corrected": "。",
            "position": 4,
            "operation": "replace",
            "context": "议结束.",
        }
    ]


def test_fullwidth_digits_with_halfwidth_dot_still_flagged():
    # 全角数字中的半角小数点不是合法数值写法（数字应整体半角），仍报标点混用
    text = "５.８亿元"
    dots = [e for e in _detect_punctuation_mixing(text) if e["original"] == "."]
    assert len(dots) == 1
    assert dots[0]["corrected"] == "。"


# ── Phase 1：errors/target 构造性一致 ─────────────────────────────


def test_apply_errors_mixed_operations():
    source = "甲乙丙丁戊"
    errors = [
        {"operation": "replace", "original": "乙", "corrected": "贰", "position": 1},
        {"operation": "delete", "original": "丁", "corrected": "", "position": 3},
        {"operation": "insert", "original": "", "corrected": "（完）", "position": 5},
    ]
    assert _apply_errors(source, errors) == "甲贰丙戊（完）"


def test_apply_errors_insert_at_start():
    assert _apply_errors(
        "开头", [{"operation": "insert", "original": "", "corrected": "注：", "position": 0}]
    ) == "注：开头"


def test_errors_target_consistency_identity_model():
    source = '"加快建设数字中国"和"一网通办"，投资5.8亿元。'
    corrector = _make_corrector([source])
    [result] = corrector.infer([source])
    assert _apply_errors(source, result["errors"]) == result["target"]
    # 引号全部成对且已应用
    assert result["target"].count("“") == result["target"].count("”") == 2
    assert '"' not in result["target"]
    # 数字内小数点豁免，未被改动
    assert "5.8亿" in result["target"]


def test_model_edit_wins_over_rule_at_same_position():
    source = "总量,约为五吨"
    model_output = "总量、约为五吨"  # 模型把半角逗号改为顿号
    corrector = _make_corrector([model_output])
    [result] = corrector.infer([source])
    comma_errors = [e for e in result["errors"] if e["position"] == 2]
    assert len(comma_errors) == 1
    assert comma_errors[0]["corrected"] == "、"
    assert "、约为" in result["target"]
    assert "，约为" not in result["target"]
    assert _apply_errors(source, result["errors"]) == result["target"]


def test_model_and_rule_errors_coexist():
    source = '惯彻"落实'
    model_output = "贯彻" + '"落实'  # 模型只改错别字，引号留给规则
    corrector = _make_corrector([model_output])
    [result] = corrector.infer([source])
    ops = sorted((e["position"], e["original"], e["corrected"]) for e in result["errors"])
    assert ops == [(0, "惯", "贯"), (2, '"', "“")]
    assert result["target"] == "贯彻“落实"


def test_only_model_edits_regression():
    # 规则零命中时 target 与模型输出一致（res_format 原语义回归）
    source = "我门今天开会。"
    model_output = "我们今天开会。"
    corrector = _make_corrector([model_output])
    [result] = corrector.infer([source])
    assert result["errors"] == [
        {
            "original": "门",
            "corrected": "们",
            "position": 1,
            "operation": "replace",
            "context": "我门今天开",
        }
    ]
    assert result["target"] == model_output


# ── 输出完整性守卫（Phase 追加：2026-08-16 第二轮日志 sess_4ee0d4de0fd1）──


def _fake_cec_response(content: str):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


def test_truncated_output_falls_back_to_identity_with_warning():
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    src = "为深入贯彻落实党的二十大关于加快建设数字中国的战略部署。" * 5  # ~150 字
    with patch.object(
        inferencer.client.chat.completions,
        "create",
        return_value=_fake_cec_response("只返回了一小段"),
    ):
        results = inferencer.infer([src])
    # 截断输出被守卫拦下：原文保留、warnings 记录
    assert results[0]["text"] == src
    assert len(results[0]["warnings"]) == 1
    assert "截断" in results[0]["warnings"][0]


def test_full_output_passes_guard_without_warning():
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    src = "为深入贯彻落实党的二十大关于加快建设数字中国的战略部署。" * 5
    out = src.replace("惯", "贯") if "惯" in src else src.replace("彻", "彻")
    with patch.object(
        inferencer.client.chat.completions, "create", return_value=_fake_cec_response(out)
    ):
        results = inferencer.infer([src])
    assert results[0]["text"] == out
    assert results[0]["warnings"] == []


def test_short_batch_skips_guard():
    # 短批次（<50 字）不做比例守卫，短输出按原样接受
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    with patch.object(
        inferencer.client.chat.completions,
        "create",
        return_value=_fake_cec_response("短输出"),
    ):
        results = inferencer.infer(["短短一句"])
    assert results[0]["text"] == "短输出"
    assert results[0]["warnings"] == []


def test_warnings_attached_to_result_items():
    corrector = _make_corrector(["原文"])
    corrector.inferencer.infer = lambda input_list: [  # type: ignore[method-assign]
        {"text": "原文", "warnings": ["某批回退"]}
    ]
    [result] = corrector.infer(["原文"])
    assert result["warnings"] == ["某批回退"]


def test_no_warnings_field_when_clean():
    corrector = _make_corrector(["原文"])
    [result] = corrector.infer(["原文"])
    assert "warnings" not in result


# ── 拆分重试（2026-08-17 部署烟测发现 max_tokens 回归后升级）──


def test_cec_call_omits_max_tokens():
    """max_tokens 不得显式传大值：端点上下文（如 8192）小于配置的
    max_length 时，prompt+max_tokens 直接 400（部署日志 sess_3cfae5b2ff0a）。"""
    from doccorrector.corrector import OpenAITextCorrectInfer

    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_cec_response("收到")

    with patch.object(
        inferencer.client.chat.completions, "create", side_effect=fake_create
    ):
        inferencer.infer(["随便一句"])
    assert "max_tokens" not in captured


def test_truncated_batch_splits_and_retries():
    """整批输出截断 → 对半拆分重试 → 拆后正常纠错（不回退原文）。"""
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    big = "句子内容五六七八九十。" * 54  # 594 字；对半 297 字 ≤ 截断阈值 300
    assert len(big) >= 400

    def fake_create(**kwargs):
        content = kwargs["messages"][0]["content"]
        batch = content[len(PROMPT_PREFIX) :]
        if len(batch) > 300:  # 大批 → 截断输出
            return _fake_cec_response("截断")
        return _fake_cec_response(batch)  # 小批 → 正常返回

    with patch.object(
        inferencer.client.chat.completions, "create", side_effect=fake_create
    ):
        [res] = inferencer.infer([big])
    assert res["text"] == big  # 拆分后全部纠错成功（恒等纠错）
    assert res["warnings"] == []


def test_prompt_too_long_400_splits():
    """prompt 超端点上下文（400 maximum context length）→ 拆分重试成功。"""
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    big = "第一段落内容。" * 80  # ~480 字

    def fake_create(**kwargs):
        content = kwargs["messages"][0]["content"]
        batch = content[len(PROMPT_PREFIX):]
        if len(batch) > 300:
            raise Exception(
                "Error code: 400 - This model's maximum context length is 8192 tokens"
            )
        return _fake_cec_response(batch)

    with patch.object(
        inferencer.client.chat.completions, "create", side_effect=fake_create
    ):
        [res] = inferencer.infer([big])
    assert res["text"] == big
    assert res["warnings"] == []


def test_split_floor_falls_back_to_identity():
    """批次已到拆分下限仍截断 → identity 兜底 + warning。"""
    inferencer = OpenAITextCorrectInfer(
        api_base="http://127.0.0.1:1", api_key="k", model_name="m"
    )
    # _split_text_by_paragraphs 会按段落合并；构造多个短段使批次 ≥400 字
    # 但每半 < 200 字不可再拆 → 直接拆一次后两半均 < 守卫下限?
    # 更直接：单段 450 字（拆一次后两半 ~225 字 > 200 仍守卫，
    # 但拆分深度内继续拆到 <200*2 停）——用恒截断输出验证最终 identity。
    batch = "字" * 450
    with patch.object(
        inferencer.client.chat.completions,
        "create",
        return_value=_fake_cec_response("短"),
    ):
        [res] = inferencer.infer([batch])
    assert res["text"] == batch  # 拆到下限仍截断 → 原文保留
    assert len(res["warnings"]) >= 1
    assert "不可再拆" in res["warnings"][0]


def test_split_batch_prefers_boundaries():
    from doccorrector.corrector import _split_batch

    a, b = _split_batch("甲乙丙丁。\n戊己庚辛。\n壬癸。\n" * 10)
    assert (a + b).count("。") == ("甲乙丙丁。\n戊己庚辛。\n壬癸。\n" * 10).count("。")
    assert a.endswith("\n") or a.endswith("。") or a.endswith("，")
    # 无任何边界的纯字串 → 字符中点硬切，长度守恒
    a, b = _split_batch("字" * 101)
    assert len(a) + len(b) == 101
