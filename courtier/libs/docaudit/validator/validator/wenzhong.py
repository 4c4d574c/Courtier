from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docmodels import Document

_WENZHONG_TITLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("决议", re.compile(r"的决议$")),
    ("决定", re.compile(r"的决定$")),
    ("命令(令)", re.compile(r"的命令$|的令$|令$")),
    ("公报", re.compile(r"公报$")),
    ("公告", re.compile(r"公告$")),
    ("通告", re.compile(r"通告$")),
    ("意见", re.compile(r"的意见$")),
    ("通知", re.compile(r"的通知$")),
    ("通报", re.compile(r"的通报$")),
    ("报告", re.compile(r"的报告$")),
    ("请示", re.compile(r"的请示$")),
    ("批复", re.compile(r"的批复$")),
    ("议案", re.compile(r"的议案$")),
    ("函", re.compile(r"的函$")),
    ("纪要", re.compile(r"纪要$")),
]

_WENZHONG_BODY_KEYWORDS: dict[str, list[str]] = {
    "通知": [
        "通知如下",
        "现通知如下",
        "特通知",
        "贯彻执行",
        "遵照执行",
        "现将有关事项通知如下",
        "通知",
    ],
    "函": ["现函告", "特函", "请予函复", "商请", "函复", "函告"],
    "请示": ["现请示如下", "妥否请批示", "请予审批", "恳请批准", "请示"],
    "报告": ["现报告如下", "特报告", "专此报告"],
    "批复": ["现批复如下", "此复"],
    "通报": ["现通报如下", "特通报"],
    "决定": ["现作如下决定", "特作如下决定"],
    "意见": ["现提出如下意见", "特提出意见"],
    "通告": ["现通告如下", "特此通告"],
    "公告": ["现予公告", "特此公告"],
    "命令(令)": ["现发布", "特令"],
    "决议": ["现作如下决议", "会议决议"],
    "议案": ["现提出议案", "提请审议"],
    "纪要": ["会议纪要", "会议决定"],
    "公报": ["全文公布"],
}


def _normalize_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = cleaned.replace("\u3000", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def detect_wenzhong(title: str, body_text: str = "") -> str | None:
    normalized_title = _normalize_text(title)

    # First pass: strict suffix match (e.g. "的通知$", "的决定$")
    for wenzhong, pattern in _WENZHONG_TITLE_PATTERNS:
        if pattern.search(normalized_title):
            return wenzhong

    # Second pass: keyword-in-title match for common patterns
    # e.g. "关于开展...检查工作的通知" contains "通知"
    _TITLE_KEYWORD_SCAN: list[tuple[str, str]] = [
        ("通知", "通知"),
        ("函", "函"),
        ("请示", "请示"),
        ("报告", "报告"),
        ("批复", "批复"),
        ("通告", "通告"),
        ("公告", "公告"),
        ("决定", "决定"),
        ("意见", "意见"),
        ("通报", "通报"),
        ("纪要", "纪要"),
        ("议案", "议案"),
    ]
    title_match: str | None = None
    for keyword, wenzhong in _TITLE_KEYWORD_SCAN:
        if keyword in normalized_title:
            title_match = wenzhong
            break

    # Third pass: body keyword match — when body strongly signals a different
    # type than the title, prefer the body (e.g. title says "请示" but body
    # says "批复").
    if body_text:
        normalized_body = _normalize_text(body_text)
        for wenzhong, keywords in _WENZHONG_BODY_KEYWORDS.items():
            for kw in keywords:
                if kw in normalized_body:
                    return wenzhong

    # Title-based match is a fallback when body text provides no signal.
    if title_match is not None:
        return title_match

    return None


def _extract_title_from_doc(doc: Document) -> str:
    for page in doc.pages:
        title_para = page.page_content.body.title
        # Body.title 为 Optional：None 表示该页未提取到标题
        if title_para is None:
            continue
        parts: list[str] = []
        for meta in title_para.elements:
            if meta.font.text:
                parts.append(meta.font.text)
        text = "".join(parts).strip()
        if text:
            return text
    return ""


def _extract_body_text_from_doc(doc: Document) -> str:
    texts: list[str] = []
    for page in doc.pages:
        body = page.page_content.body
        for para in body.main_text:
            parts: list[str] = []
            for meta in para.elements:
                if meta.font.text:
                    parts.append(meta.font.text)
            text = "".join(parts).strip()
            if text:
                texts.append(text)
    return "\n".join(texts)


def detect_wenzhong_from_doc(doc: Document) -> str | None:
    title = _extract_title_from_doc(doc)
    body_text = _extract_body_text_from_doc(doc)
    return detect_wenzhong(title, body_text)
