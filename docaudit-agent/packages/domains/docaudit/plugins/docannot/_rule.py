from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """关键词到批注的映射规则。

    每当 keyword 在段落或表格单元格中被找到，
    comment 将作为 Word 批注附加，作者为 author。
    """

    keyword: str
    comment: str
    author: str = "docannot"

    def __post_init__(self) -> None:
        if not self.keyword:
            raise ValueError("keyword must be non-empty")
