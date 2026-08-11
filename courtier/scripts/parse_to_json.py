"""批量解析文档为 Document JSON。

用 docparse 的统一入口（registry 自动分派：文本 PDF / 扫描件 OCR+LLM /
DOCX / 图片）将指定文件或目录下的所有支持文件解析为 Document 对象，
并以 JSON 保存（每份文件一个 .json）。

原始响应字段（ocr_raw、raw 等 OCR/LLM 原始返回）不写入 JSON：当前
Document 模型本就不携带这些字段，此处仍按排除表递归剔除，以防旧模型
残留字段或未来新增字段泄漏进输出。可用 --exclude 追加排除键。

用法:
    # 单文件
    uv run python scripts/parse_to_json.py /path/to/文件.pdf

    # 目录（仅顶层）；-r 递归子目录
    uv run python scripts/parse_to_json.py /path/to/目录 -o output/ -r

    # 追加排除键；保留 null 槽位；指定 env 文件
    uv run python scripts/parse_to_json.py in/ --exclude warnings --keep-null --env-file .env

扫描件路径需要 LLM_API_KEY / LLM_IP / LLM_NAME / DOCPARSE_OCR_API_URL 等
环境变量；默认自动加载 courtier/.env（不覆盖已有环境变量）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from docparse import parse  # noqa: E402
from docparse.parsers._constants import IMAGE_EXTENSIONS  # noqa: E402

logger = logging.getLogger("parse_to_json")

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx"}) | IMAGE_EXTENSIONS

# 默认剔除的原始数据键（递归生效）。Document 当前不含这些字段，剔除是防御性的。
DEFAULT_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {"ocr_raw", "raw", "raw_response", "ocr_result", "llm_raw"}
)


def _strip_excluded_keys(obj: Any, excluded: frozenset[str]) -> Any:
    """递归删除 dict 中名为 excluded 的键，list/标量原样递归。"""
    if isinstance(obj, dict):
        return {k: _strip_excluded_keys(v, excluded) for k, v in obj.items() if k not in excluded}
    if isinstance(obj, list):
        return [_strip_excluded_keys(item, excluded) for item in obj]
    return obj


def _load_env_file(env_file: Path) -> None:
    """加载 .env（不覆盖已存在的环境变量）。文件不存在时静默跳过。"""
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv 不可用，跳过 env 文件加载：%s", env_file)
        return
    load_dotenv(env_file, override=False)
    logger.debug("已加载 env 文件：%s", env_file)


def _collect_inputs(source: Path, recursive: bool) -> list[Path]:
    """收集待解析文件列表。source 为文件时直接返回（校验后缀）。"""
    if source.is_file():
        return [source]
    pattern = "**/*" if recursive else "*"
    files = [
        p
        for p in sorted(source.glob(pattern))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return files


def _output_path_for(
    input_path: Path,
    source_root: Path,
    output_dir: Path,
    recursive: bool,
    used_names: set[str],
) -> Path:
    """计算输出 JSON 路径。

    递归模式镜像相对目录结构；平铺模式用文件名。同名冲突时追加序号。
    """
    if recursive and source_root.is_dir():
        rel = input_path.relative_to(source_root)
        candidate = output_dir / rel.parent / f"{input_path.stem}.json"
    else:
        candidate = output_dir / f"{input_path.stem}.json"

    key = str(candidate)
    counter = 2
    while key in used_names:
        candidate = candidate.with_name(f"{input_path.stem}__{counter}.json")
        key = str(candidate)
        counter += 1
    used_names.add(key)
    return candidate


def _parse_one(
    input_path: Path,
    excluded: frozenset[str],
    exclude_none: bool,
) -> dict[str, Any]:
    """解析单个文件并返回可序列化的 Document dict。"""
    doc = parse(str(input_path))
    data = doc.model_dump(mode="json", exclude_none=exclude_none)
    return _strip_excluded_keys(data, excluded)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将文件/目录中的文档解析为 Document 对象并保存为 JSON（剔除原始响应字段）。"
    )
    parser.add_argument("source", help="输入文件或目录")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="parsed_documents",
        help="输出目录（默认 ./parsed_documents）",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument(
        "--env-file",
        default=str(PROJECT_ROOT / ".env"),
        help="env 文件路径（默认 courtier/.env，不存在则跳过）",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="KEY",
        help="追加需要从 JSON 中剔除的键（可重复）",
    )
    parser.add_argument(
        "--keep-null",
        action="store_true",
        help="保留值为 null 的槽位（默认省略，与 parse 插件输出一致）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        logger.error("输入不存在：%s", source)
        return 2
    if source.is_file() and source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        logger.error(
            "不支持的文件类型：%s（支持：%s）",
            source.suffix,
            ", ".join(sorted(SUPPORTED_EXTENSIONS)),
        )
        return 2

    _load_env_file(Path(args.env_file).expanduser())

    inputs = _collect_inputs(source, args.recursive)
    if not inputs:
        logger.error(
            "目录下未找到可解析文件（支持：%s）：%s",
            ", ".join(sorted(SUPPORTED_EXTENSIONS)),
            source,
        )
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve()
    excluded = DEFAULT_EXCLUDED_KEYS | frozenset(args.exclude)

    used_names: set[str] = set()
    succeeded = 0
    failed: list[tuple[Path, str]] = []

    for input_path in inputs:
        out_path = _output_path_for(input_path, source, output_dir, args.recursive, used_names)
        try:
            data = _parse_one(input_path, excluded, exclude_none=not args.keep_null)
        except Exception as exc:
            failed.append((input_path, str(exc)))
            logger.error("[FAIL] %s: %s", input_path, exc)
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        succeeded += 1
        logger.info(
            "[OK] %s -> %s（页数=%d，来源=%s，告警=%d）",
            input_path,
            out_path,
            data.get("total_page_num", 0),
            data.get("source") or "?",
            len(data.get("warnings", [])),
        )

    logger.info("完成：成功 %d，失败 %d，输出目录 %s", succeeded, len(failed), output_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
