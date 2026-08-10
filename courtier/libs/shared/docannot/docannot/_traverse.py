from collections.abc import Iterator

from docxnote import Paragraph, Table


def iter_paragraphs(
    blocks: tuple[Paragraph | Table, ...],
) -> Iterator[Paragraph]:
    """递归遍历块级元素，yield 所有 Paragraph（含表格单元格内的段落）。"""
    for block in blocks:
        if isinstance(block, Paragraph):
            yield block
        elif isinstance(block, Table):
            rows, cols = block.shape()
            for r in range(rows):
                for c in range(cols):
                    yield from iter_paragraphs(block[r, c].blocks())
