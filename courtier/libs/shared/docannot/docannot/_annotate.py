import base64
from collections.abc import Sequence
from pathlib import Path

from docxnote import DocxDocument

from ._rule import Rule
from ._traverse import iter_paragraphs


def _try_base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return None


def _is_within(path: Path, dirs: Sequence[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(d.resolve()) for d in dirs)


def annotate(
    source: bytes | str | Path,
    rules: Sequence[Rule],
    *,
    keep_comments: bool = True,
    allowed_dirs: Sequence[Path] | None = None,
) -> bytes:
    """对 DOCX 文档按关键词规则添加批注。

    Args:
        source: DOCX 文件的原始字节，或文件路径（str / Path）。
        rules: 批注规则列表。每条规则的 keyword 在段落或表格单元格中
               每次出现时都会附加对应 comment。
        keep_comments: 是否保留文档中已有批注。默认 True。
        allowed_dirs: 可选的安全目录列表，文件路径必须在其中。

    Returns:
        批注后的 DOCX 文件内容（bytes）。
    """
    data: bytes
    if isinstance(source, bytes):
        data = source
    elif isinstance(source, Path):
        source = source.resolve()
        if allowed_dirs and not _is_within(source, allowed_dirs):
            raise ValueError(f"Path outside allowed directories: {source}")
        data = source.read_bytes()
    elif isinstance(source, str):
        decoded = _try_base64(source)
        if decoded is not None:
            data = decoded
        else:
            p = Path(source).resolve()
            if allowed_dirs and not _is_within(p, allowed_dirs):
                raise ValueError(f"Path outside allowed directories: {source}")
            data = p.read_bytes()
    else:
        raise TypeError(f"Unsupported source type: {type(source)}")

    doc = DocxDocument.parse(data, keep_comments=keep_comments)

    for paragraph in iter_paragraphs(doc.blocks()):
        text = paragraph.text
        for rule in rules:
            start = 0
            while (idx := text.find(rule.keyword, start)) != -1:
                paragraph.comment(
                    rule.comment,
                    start=idx,
                    end=idx + len(rule.keyword),
                    author=rule.author,
                )
                start = idx + len(rule.keyword)

    return doc.render()
