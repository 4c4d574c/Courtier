"""中文拼写和语法错误纠正 — 单文件版本

六阶段流水线：
  阶段一：用户词典加载
  阶段二：4B 模型纠错
  阶段三-a：重复字检测
  阶段三-b：全角半角检测
  阶段三-c：标点语种混用检测
  阶段三-d：截断人名检测
  阶段四：冲突消解（保护词兜底 + 模型/规则重叠消解）
  阶段五：target 构造（errors 确定性应用到 source）

结果契约：每条结果的 target 由 errors 应用生成（target == 在 source 上
应用全部 errors），两者必然一致；若纠错模型输出异常截断，对应批次按
原文保留，该条结果带 warnings 字段说明未纠错的批次。
"""

import difflib
import logging
import re
from pathlib import Path
from typing import Any, cast

import httpx
from openai import OpenAI

# ── 常量 ────────────────────────────────

PROMPT_PREFIX: str = (
    "你是一个文本纠错专家，纠正输入句子中的语法错误，并输出正确的句子，输入句子为："
)

# 合法叠词（不受配置影响）
_DEFAULT_ALLOWED_PATTERNS: set[str] = {"看一看", "想一想", "试一试", "人人", "一一"}

# 输出完整性守卫：模型输出低于输入批次的该比例视为截断/异常，该批回退为原文。
# 纠错器不应大幅缩短文本——若 diff 把未输出内容全部标为删除，会静默丢文本。
_MIN_OUTPUT_RATIO: float = 0.5
# 批次过短时不做守卫（几十字的输入输出比例无意义）。
_MIN_BATCH_CHARS_FOR_GUARD: int = 50

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 阶段一：用户词典加载与保护词区间计算
# ═══════════════════════════════════════════════════════════════════


def _load_user_words(path: str) -> set[str]:
    """从文件加载禁止改动的词表，每行一个词"""
    if not path or not Path(path).exists():
        return set()
    words = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(line)
    logger.info("加载用户词典 %s，共 %d 个保护词", path, len(words))
    return words


def _build_protected_ranges(text: str, words: set[str]) -> list[tuple[int, int]]:
    """返回所有保护词在原文中的 (start, end) 区间列表"""
    ranges: list[tuple[int, int]] = []
    for word in words:
        start = 0
        while True:
            pos = text.find(word, start)
            if pos == -1:
                break
            ranges.append((pos, pos + len(word)))
            start = pos + 1
    return sorted(ranges, key=lambda x: x[0])


def _is_overlapping(range1: tuple[int, int], range2: tuple[int, int]) -> bool:
    """判断两个区间是否有交集"""
    return range1[0] < range2[1] and range2[0] < range1[1]


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════


def res_format(sentences: list[str], result: list[str]) -> list[dict[str, str | list[dict]]]:
    """对比原文与纠错结果，提取错误标注（含操作类型和前后文）"""
    if len(sentences) != len(result):
        logger.warning(
            "纠错结果数量(%d)与输入数量(%d)不一致，将以较短的为准",
            len(result),
            len(sentences),
        )
    data: list[dict[str, str | list[dict]]] = []
    for a, b in zip(sentences, result):
        if not a or not b or a == "\n":
            continue
        errors: list[dict] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if tag != "equal":
                ctx_start = max(0, i1 - 3)
                ctx_end = min(len(a), i2 + 3)
                context = a[ctx_start:ctx_end]
                errors.append(
                    {
                        "original": a[i1:i2],
                        "corrected": b[j1:j2],
                        "position": i1,
                        "operation": tag,
                        "context": context,
                    }
                )
        data.append({"source": a, "target": b, "errors": errors})
    return data


# ═══════════════════════════════════════════════════════════════════
# 阶段三：重复字/缺字规则
# ═══════════════════════════════════════════════════════════════════


def _detect_duplicate_chars(text: str) -> list[dict]:
    """检测连续重复汉字（叠字），返回 delete 类型 errors"""
    errors: list[dict] = []
    for m in re.finditer(r"([\u4e00-\u9fff])\1+", text):
        dup = m.group(0)  # 如"款款"、"了了"
        # 跳过合法叠词
        if dup in _DEFAULT_ALLOWED_PATTERNS:
            continue
        # 只保留第二个开始的重复字（第一个是合法的）
        original = dup[1:]
        if not original:
            continue
        position = m.start() + 1  # 从第二个重复字开始
        ctx_start = max(0, position - 3)
        ctx_end = min(len(text), position + len(original) + 3)
        context = text[ctx_start:ctx_end]
        errors.append(
            {
                "original": original,
                "corrected": "",
                "position": position,
                "operation": "delete",
                "context": context,
            }
        )
    return errors


def _pycorrector_check(text: str) -> list[dict]:
    """用 pycorrector 做额外检测（可选，依赖 torch，缺失时静默跳过）"""
    errors: list[dict] = []
    try:
        from pycorrector import Corrector as _Corrector

        _, details = _Corrector().correct(text)
        for detail in details:
            err_type = detail.get("type", "")
            if err_type in (" redundancy", "redundancy", "重复"):
                idx = detail.get("idx", [])
                if not idx:
                    continue
                start, end = idx[0], (idx[1] if len(idx) > 1 else idx[0] + 1)
                original = text[start:end]
                errors.append(
                    {
                        "original": original,
                        "corrected": "",
                        "position": start,
                        "operation": "delete",
                        "context": text[max(0, start - 3) : min(len(text), end + 3)],
                    }
                )
    except ImportError:
        # pycorrector 依赖 torch，torch 未安装时静默跳过，不影响主流程
        pass
    except Exception:
        logger.warning("pycorrector check failed, skipping", exc_info=True)
    return errors


# ═══════════════════════════════════════════════════════════════════
# 阶段三-b：全角半角检测
# ═══════════════════════════════════════════════════════════════════

# 全角 → 半角映射表
# 全角 Latin 字母/数字的 Unicode 区间：
#   Ａ-Ｚ: U+FF21～U+FF3A, ａ-ｚ: U+FF41～U+FF5A
#   ０-９: U+FF10～U+FF19
# 统一偏移量 = 0xFF21 - ord('A') = 0xFEE0
_FW_TO_HW_OFFSET: int = 0xFF21 - ord("A")  # = 0xFEE0

# 全角标点字符集合（用于 _detect_fullwidth_chars 区分字母/数字和标点）
_FW_PUNC_CHARS = frozenset(
    {"、", "。", "；", "：", "！", "？", "（", "）", "【", "】", "《", "》", "，"}
)


def _fw_to_hw(ch: str) -> str | None:
    """将单个全角字符转为半角，返回 None 表示无需转换"""
    cp = ord(ch)
    # 全角字母：\uFF21-\uFF3A (Ａ-Ｚ), \uFF41-\uFF5A (ａ-ｚ)
    if 0xFF21 <= cp <= 0xFF3A:
        return chr(cp - _FW_TO_HW_OFFSET)
    if 0xFF41 <= cp <= 0xFF5A:
        return chr(cp - _FW_TO_HW_OFFSET)
    # 全角数字：\uFF10-\uFF19 (０-９)
    if 0xFF10 <= cp <= 0xFF19:
        return chr(cp - _FW_TO_HW_OFFSET)
    # 全角标点（可选启用）：，。；：！？""''（）【】
    fw_punc_map = {
        "，": ",",
        "。": ".",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "、": ",",
    }
    return fw_punc_map.get(ch, None)


def _detect_fullwidth_chars(text: str, check_punc: bool = False) -> list[dict]:
    """检测全角字符，标记为 replace 错误。

    注意：政府公文标准是全角标点，此函数只检测全角字母和数字，
    标点混用由 _detect_punctuation_mixing 处理（半角改全角）。

    Args:
        text: 待检测文本
        check_punc: 是否检测全角标点（默认 False，全角是目标格式，不报错）
    """
    errors: list[dict] = []
    for i, ch in enumerate(text):
        hw = _fw_to_hw(ch)
        if hw is None:
            continue
        # 只检测字母和数字，标点混用由 _detect_punctuation_mixing 处理
        if not check_punc and ch in _FW_PUNC_CHARS:
            continue
        ctx_start = max(0, i - 3)
        ctx_end = min(len(text), i + 4)
        errors.append(
            {
                "original": ch,
                "corrected": hw,
                "position": i,
                "operation": "replace",
                "context": text[ctx_start:ctx_end],
            }
        )
    return errors


# ═══════════════════════════════════════════════════════════════════
# 阶段三-c：标点语种混用检测
# ═══════════════════════════════════════════════════════════════════


# 半角 → 全角 映射（引号除外，见 _QUOTE_PAIRS 开闭配对）
_HW_TO_FW = {
    ",": "，",
    ".": "。",
    ";": "；",
    ":": "：",
    "!": "！",
    "?": "？",
    "(": "（",
    ")": "）",
    "[": "【",
    "]": "】",
    "<": "《",
    ">": "》",
}

# 半角引号 → 弯引号对：按出现顺序开闭交替（GB/T 15834，公文统一全角标点）
_QUOTE_PAIRS: dict[str, tuple[str, str]] = {'"': ("“", "”"), "'": ("‘", "’")}

# 半角数字字符集（用于数字内小数点豁免判断）
_ASCII_DIGITS = frozenset("0123456789")


def _detect_punctuation_mixing(text: str) -> list[dict]:
    """检测标点语种混用，标记为 replace 错误。

    政府公文标准：统一使用全角标点

    规则：
      - 检测所有半角标点，建议改为全角
      - 标点混用时，全部半角标点都标记为错误
      - 半角引号按出现顺序开闭交替转为弯引号（“”、‘’）
      - 数字内小数点（前后均为半角数字，如 5.8 亿元）豁免——
        GB/T 15835-2011 规定数字中的小数点使用半角句点
    """
    errors: list[dict] = []
    # False = 下一处该字符期望开引号
    quote_state: dict[str, bool] = {'"': False, "'": False}

    for i, ch in enumerate(text):
        if ch in _QUOTE_PAIRS:
            open_q, close_q = _QUOTE_PAIRS[ch]
            corrected = open_q if not quote_state[ch] else close_q
            quote_state[ch] = not quote_state[ch]
        elif ch in _HW_TO_FW:
            if (
                ch == "."
                and 0 < i < len(text) - 1
                and text[i - 1] in _ASCII_DIGITS
                and text[i + 1] in _ASCII_DIGITS
            ):
                continue
            corrected = _HW_TO_FW[ch]
        else:
            continue
        ctx_start = max(0, i - 3)
        ctx_end = min(len(text), i + 4)
        errors.append(
            {
                "original": ch,
                "corrected": corrected,
                "position": i,
                "operation": "replace",
                "context": text[ctx_start:ctx_end],
            }
        )

    return errors


# ═══════════════════════════════════════════════════════════════════
# 阶段三-d：正向纠错 — 检测截断人名
# ═══════════════════════════════════════════════════════════════════


def _find_truncated_forms(
    source: str,
    protected_words: set[str],
    max_missing: int = 2,
) -> list[dict]:
    """检测原文中的截断人名，标记为 replace。

    核心思路：枚举每个完整词的子字符串，
    在原文中查找这些子串，如果子串出现但完整词未出现，则触发。

    为避免组合爆炸，只枚举"首+末固定、中间缺失1~max_missing字"的情况。

    例：词典含"王小洪"（3字），max_missing=2
    → 枚举子串："王洪"（缺1字）、"王?洪"（缺1字pattern）
    → 在原文中找到"王洪"但没有"王小洪" → 标记
    """
    errors: list[dict] = []
    seen_keys: set[tuple[str, int]] = set()

    for word in protected_words:
        if len(word) < 2:
            continue
        # 枚举缺失1~max_missing个字的情况
        for missing in range(1, max_missing + 1):
            if len(word) - missing < 2:  # 截断后至少保留2字
                break
            # 跳过的中间部分长度 = missing
            for skip_start in range(1, len(word) - missing):
                # 构造截断形式：保留前缀 word[:skip_start]，跳过 missing 字，接后缀
                trunc = word[:skip_start] + word[skip_start + missing :]
                # 在原文中查找截断形式
                pos = source.find(trunc)
                if pos == -1:
                    continue
                # 完整词已存在于原文，不触发
                if word in source:
                    continue
                # 去重
                key = (trunc, pos)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                ctx_start = max(0, pos - 3)
                ctx_end = min(len(source), pos + len(trunc) + 3)
                errors.append(
                    {
                        "original": trunc,
                        "corrected": word,
                        "position": pos,
                        "operation": "replace",
                        "context": source[ctx_start:ctx_end],
                    }
                )
    return errors


# ═══════════════════════════════════════════════════════════════════
# 阶段五：冲突消解 — 剔除模型误伤的保护词
# ═══════════════════════════════════════════════════════════════════


def _protect_corrected_text(
    errors: list[dict],
    source: str,
    protected_words: set[str],
) -> list[dict]:
    """剔除纠错后文本中破坏了受保护人名/词组的 errors。

    两种场景需要剔除：
    1. delete：将原文的受保护词截断了（如"王小洪"→删了"小"字→"王洪"）
    2. replace：将受保护词的一部分替换掉了（如"王小洪"→"王大海"）
    """
    if not protected_words:
        return errors

    resolved: list[dict] = []
    for e in errors:
        op = e.get("operation", "")
        pos = e.get("position", 0)
        original = e.get("original", "")
        corrected = e.get("corrected", "")

        if op == "delete":
            after = source[:pos] + source[pos + len(original) :]
        elif op == "replace":
            after = source[:pos] + corrected + source[pos + len(original) :]
        elif op == "insert":
            after = source[:pos] + corrected + source[pos:]
        else:
            after = None

        hurt = False
        if after is not None:
            for word in protected_words:
                if word in source and word not in after:
                    hurt = True
                    logger.debug(
                        "剔除 error: 操作截断了受保护词 '%s' " "(op=%s original='%s' position=%d)",
                        word,
                        op,
                        original,
                        pos,
                    )
                    break

        if not hurt:
            resolved.append(e)

    return resolved


def _resolve_conflicts(
    errors: list[dict],
    source: str,
    protected_words: set[str],
    protected_ranges: list[tuple[int, int]],
) -> list[dict]:
    """两层冲突消解：原文区间 + 纠错后文本完整性"""
    resolved: list[dict] = []
    for e in errors:
        pos = e.get("position", 0)
        original = e.get("original", "")
        err_range = (pos, pos + len(original)) if original else (pos, pos + 1)
        hurt = False
        for pr in protected_ranges:
            if _is_overlapping(err_range, pr):
                hurt = True
                logger.debug(
                    "阶段四-1 剔除: 位置=%d original='%s' 命中保护词区间 %s-%s",
                    pos,
                    original,
                    pr[0],
                    pr[1],
                )
                break
        if not hurt:
            resolved.append(e)
    return _protect_corrected_text(resolved, source, protected_words)


# ═══════════════════════════════════════════════════════════════════
# 阶段五：target 构造与区间重叠判断
# ═══════════════════════════════════════════════════════════════════


def _error_range(e: dict) -> tuple[int, int]:
    """该 error 在原文中的 [start, end) 区间；零宽（insert）按单点处理。"""
    pos = e.get("position", 0)
    width = len(e.get("original", "") or e.get("corrected", "") or "")
    return (pos, pos + max(width, 1))


def _overlaps_any(e: dict, refs: list[dict]) -> bool:
    """判断 error e 的原文区间是否与 refs 中任一 error 相交。"""
    start, end = _error_range(e)
    for r in refs:
        r_start, r_end = _error_range(r)
        if start < r_end and r_start < end:
            return True
    return False


def _apply_errors(source: str, errors: list[dict]) -> str:
    """按 errors 列表把修正应用到 source，构造最终 target。

    errors 须已按 position 升序且区间两两不重叠。单次从左到右应用，
    position 语义始终基于原 source（与 res_format 的 difflib 坐标一致）。
    """
    parts: list[str] = []
    cursor = 0  # source 中下一未消费位置
    for e in errors:
        op = e.get("operation", "replace")
        pos = e.get("position", 0)
        original = e.get("original", "")
        corrected = e.get("corrected", "")
        parts.append(source[cursor:pos])
        if op == "insert":
            parts.append(corrected)
            cursor = pos
        else:
            parts.append(corrected)  # delete 时 corrected=""
            cursor = pos + len(original)
    parts.append(source[cursor:])
    return "".join(parts)


# ── 推理类 ─────────────────────────────────────────────────────────


class OpenAITextCorrectInfer:
    """基于 OpenAI 兼容接口的文本纠错推理器"""

    def __init__(
        self,
        api_base: str = "",
        api_key: str = "",
        model_name: str = "",
        max_length: int = 16383,
        max_input_chars: int = 8000,
    ) -> None:
        if not api_base:
            raise ValueError(
                "api_base is required for ChineseErrorCorrector. "
                "Configure CEC_API_BASE in your environment."
            )
        self.client = OpenAI(
            api_key=api_key or "EMPTY",
            base_url=api_base,
            http_client=httpx.Client(
                proxy=None,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            ),
        )
        self.model = model_name or "ChineseErrorCorrector3-4B"
        self.max_length = max_length
        self.max_input_chars = max_input_chars

    def _split_text_by_paragraphs(self, text: str, max_chars: int | None = None) -> list[str]:
        if max_chars is None:
            max_chars = self.max_input_chars
        """将文本按段落切分为多个批次，每批不超过 max_chars 字符"""
        paragraphs = text.split("\n")
        batches: list[str] = []
        current_batch: list[str] = []
        current_len = 0

        for para in paragraphs:
            para = para.rstrip("\r")
            para_len = len(para)

            # 兜底：单个段落超长则按字符强制切分
            if para_len > max_chars:
                if current_batch:
                    batches.append("\n".join(current_batch))
                    current_batch = []
                    current_len = 0
                for i in range(0, para_len, max_chars):
                    batches.append(para[i : i + max_chars])
                continue

            if not current_batch:
                current_batch = [para]
                current_len = para_len
            else:
                new_len = current_len + 1 + para_len  # +1 for \n
                if new_len > max_chars:
                    batches.append("\n".join(current_batch))
                    current_batch = [para]
                    current_len = para_len
                else:
                    current_batch.append(para)
                    current_len = new_len

        if current_batch:
            batches.append("\n".join(current_batch))

        return batches

    def infer(self, input_list: list[str]) -> list[dict]:
        """返回 [{text, warnings}]——text 为纠错后文本，warnings 为批次级异常提示。"""
        results: list[dict] = []
        for query in input_list:
            batches = self._split_text_by_paragraphs(query)
            batch_results: list[str] = []
            warnings: list[str] = []
            for batch in batches:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": PROMPT_PREFIX + batch}],
                    temperature=0.6,
                    top_p=0.95,
                    max_tokens=self.max_length,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                text = response.choices[0].message.content
                out = text.strip() if text else ""
                # 输出完整性守卫：纠错器不应大幅缩短文本。模型输出远短于
                # 输入批次时（如输出长度被服务端截断），diff 会把其余内容
                # 全部标记为删除——宁可不纠错，也不能静默丢文本。
                batch_chars = len(batch.strip())
                if (
                    batch_chars >= _MIN_BATCH_CHARS_FOR_GUARD
                    and len(out) < batch_chars * _MIN_OUTPUT_RATIO
                ):
                    logger.warning(
                        "CEC 模型输出疑似截断：输入 %d 字，输出 %d 字，该批按原文保留",
                        batch_chars,
                        len(out),
                    )
                    warnings.append(
                        f"纠错模型输出疑似截断（输出 {len(out)} 字 < 输入 "
                        f"{batch_chars} 字的 {_MIN_OUTPUT_RATIO:.0%}），"
                        "该批次已按原文保留、未做模型纠错（规则类检查仍生效）。"
                    )
                    batch_results.append(batch)
                else:
                    batch_results.append(out)
            results.append({"text": "\n".join(batch_results), "warnings": warnings})
        return results


# ═══════════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════════


class ErrorCorrect:
    """中文拼写和语法错误纠正 — 五阶段流水线

    配置全部通过构造参数显式传入；库本身不读取应用配置或环境变量，
    由调用方（如 text_correction 插件）负责从环境注入。

    结果契约：每条结果的 target 由 errors 确定性应用生成
    （target == 在 source 上应用全部 errors），两者必然一致。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str = "",
        model_name: str = "ChineseErrorCorrector3-4B",
        max_length: int = 16383,
        user_dict: str = "",
    ) -> None:
        self.inferencer = OpenAITextCorrectInfer(
            api_base=api_base,
            api_key=api_key,
            model_name=model_name,
            max_length=max_length,
            max_input_chars=max_length // 2,
        )
        self._protected_words: set[str] = _load_user_words(user_dict)

    def infer(self, input_list: list[str]) -> list[dict]:
        # ── 阶段二：4B 模型纠错（模型 diff 误差为权威，规则不与模型争抢）──
        res = self.inferencer.infer(input_list)
        outputs = [cast(str, r.get("text", "")) for r in res]
        warnings_by_input = [cast(list[str], r.get("warnings", [])) for r in res]
        results = res_format(input_list, outputs)
        for item, warns in zip(results, warnings_by_input):
            if warns:
                item["warnings"] = warns

        for item in results:
            source = cast(str, item["source"])
            model_errors = cast(list[dict[str, Any]], item["errors"])

            # ── 阶段三：规则补充 ──────────────────────────────────
            # 重复字检测
            dup_errors = _detect_duplicate_chars(source)
            # 全角半角检测
            fw_errors = _detect_fullwidth_chars(source)
            # 标点语种混用检测
            punc_errors = _detect_punctuation_mixing(source)
            # pycorrector 补充检测
            py_errors = _pycorrector_check(source)
            # 正向纠错：检测截断的人名（如"王洪"→"王小洪"）
            trunc_errors = _find_truncated_forms(source, self._protected_words)
            rule_errors = dup_errors + fw_errors + punc_errors + py_errors + trunc_errors

            # ── 阶段四：冲突消解（保护词两层兜底 + 重叠消解）──
            protected_ranges = _build_protected_ranges(source, self._protected_words)
            resolved = _resolve_conflicts(
                model_errors,
                source,
                self._protected_words,
                protected_ranges,
            )
            rule_resolved = _resolve_conflicts(
                rule_errors,
                source,
                self._protected_words,
                protected_ranges,
            )
            # 模型已改的位置规则不再重复报（模型 diff 优先）；规则内部
            # 重叠（如重复字检测与 pycorrector 双报）按位置靠前者保留。
            rule_kept: list[dict] = []
            for e in sorted(rule_resolved, key=lambda e: e.get("position", 0)):
                if not _overlaps_any(e, resolved) and not _overlaps_any(e, rule_kept):
                    rule_kept.append(e)
            resolved.extend(rule_kept)
            resolved.sort(key=lambda e: e.get("position", 0))

            # ── 阶段五：构造性一致——target 由最终 errors 应用生成 ──
            item["errors"] = resolved
            item["target"] = _apply_errors(source, resolved)

        return results
