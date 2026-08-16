"""Structure parsing for long documents — outline navigation.

Pure functions, no dependencies: convert markdown/official-document text
into a list of sections (headings with levels, char offsets and lengths)
so agents can navigate a persisted ``$ref`` result — outline first, then
section reads — instead of blind token-window paging.

Heading recognition covers two channels:

- ATX markdown: ``#`` … ``######`` (levels capped at 3 for outline depth).
- Official-document numbering: 第X章 (L1), 第X节 (L2), 一、 (L2),
  （一） (L3).

Text without any recognised headings degrades to fixed 4000-char
"块 N" pseudo-sections so an outline always exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Chinese numerals up to 99 (enough for chapter/section numbering).
_CN_NUM = "[一二三四五六七八九十百]+"
_CN_SMALL = "[一二三四五六七八九十]+"

_NUM = "[一二三四五六七八九十百0-9]+"
_HEADING_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^(#{1,6})\s+(.+)"), 0),  # ATX: level derived from hash count
    (re.compile(rf"^第{_NUM}章\s*(.*)"), 1),
    (re.compile(rf"^第{_NUM}节\s*(.*)"), 2),
    (re.compile(rf"^第{_NUM}条"), -1),  # article-level: NOT an outline heading
    (re.compile(rf"^({_CN_SMALL})、\s*(.*)"), 2),
    (re.compile(rf"^（({_CN_SMALL})）\s*(.*)"), 3),
]

#: Degradation window for heading-less documents.
_BLOCK_CHARS = 4000

_CN_NUM_VALUE = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(text: str) -> int | None:
    """Convert 中文数字 (一..九十九) to int; None when unparseable."""
    t = text.strip()
    if t.isdigit():
        return int(t)
    if not t or not all(c in _CN_NUM_VALUE for c in t):
        return None
    if t == "十":
        return 10
    if len(t) == 1:
        return _CN_NUM_VALUE[t]
    if t.startswith("十"):
        return 10 + _CN_NUM_VALUE.get(t[1], 0)
    if t.endswith("十"):
        return _CN_NUM_VALUE.get(t[0], 0) * 10
    if "十" in t:
        head, tail = t.split("十", 1)
        return _CN_NUM_VALUE.get(head, 0) * 10 + _CN_NUM_VALUE.get(tail, 0)
    return None


def _selector_to_index(selector: str) -> int | None:
    """Parse a section selector like "3", "3.", "第3节" into an outline index."""
    s = selector.strip().rstrip(".")
    digits = re.match(r"^(\d+)$", s)
    if digits:
        return int(digits.group(1))
    cn = re.match(rf"^第?({_CN_NUM}|[0-9]+)[章节]?$", s)
    if cn:
        raw = cn.group(1)
        return int(raw) if raw.isdigit() else _cn_to_int(raw)
    return None


@dataclass(frozen=True)
class Section:
    """One outline section: heading + char range [start_char, end_char)."""

    index: int
    level: int
    title: str
    start_char: int
    end_char: int
    chars: int

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "level": self.level,
            "title": self.title,
            "start_char": self.start_char,
            "chars": self.chars,
        }


@dataclass(frozen=True)
class _Heading:
    """Internal: a recognised heading occurrence."""

    level: int
    title: str
    start: int


def parse_sections(text: str) -> list[Section]:
    """Split *text* into sections.

    A section runs from its heading to the next heading of the same or
    higher level.  Heading-less text degrades to fixed-window blocks.
    """
    if not text:
        return []

    lines = text.splitlines()
    headings: list[_Heading] = []
    offset = 0
    for raw_line in lines:
        line = raw_line.strip()
        line_start = offset + (len(raw_line) - len(raw_line.lstrip()))
        offset += len(raw_line) + 1  # +1 for the newline

        matched: _Heading | None = None
        for pattern, level_hint in _HEADING_PATTERNS:
            m = pattern.match(line)
            if m is None:
                continue
            if level_hint == -1:
                matched = None  # 第X条 — not an outline heading
                break
            if level_hint == 0:
                level = min(len(m.group(1)), 3)
                title = m.group(2).strip()
            else:
                level = level_hint
                # Chinese-style headings keep their numbering in the title.
                title = line.strip()
            matched = _Heading(level=level, title=title, start=line_start)
            break
        if matched is not None:
            headings.append(matched)

    if not headings:
        # Degradation: fixed-window pseudo sections.
        sections: list[Section] = []
        for i in range(0, len(text), _BLOCK_CHARS):
            end = min(i + _BLOCK_CHARS, len(text))
            idx = len(sections) + 1
            sections.append(
                Section(
                    index=idx,
                    level=1,
                    title=f"块 {idx}",
                    start_char=i,
                    end_char=end,
                    chars=end - i,
                )
            )
        return sections

    # Close each section at the next heading of the same or higher level.
    sections = []
    for i, heading in enumerate(headings):
        end = len(text)
        for later in headings[i + 1 :]:
            if later.level <= heading.level:
                end = later.start
                break
        sections.append(
            Section(
                index=len(sections) + 1,
                level=heading.level,
                title=heading.title,
                start_char=heading.start,
                end_char=end,
                chars=end - heading.start,
            )
        )
    return sections


def find_section(sections: list[Section], selector: str) -> Section | None:
    """Locate a section by outline index or heading text.

    Matching order: exact title match, index (arabic/chinese numerals,
    with or without 第/章/节 decoration), then title containment.
    """
    if not sections:
        return None
    sel = selector.strip()
    for section in sections:
        if section.title == sel:
            return section
    idx = _selector_to_index(sel)
    if idx is not None:
        for section in sections:
            if section.index == idx:
                return section
    for section in sections:
        if sel and sel in section.title:
            return section
    return None
