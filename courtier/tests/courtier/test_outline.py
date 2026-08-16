"""Tests for the outline/section parser (artifacts.outline)."""

from courtier.agent.artifacts.outline import (
    _cn_to_int,
    _selector_to_index,
    find_section,
    parse_sections,
)


def _titles(text: str) -> list[str]:
    return [s.title for s in parse_sections(text)]


def _ranges(text: str) -> list[tuple[int, int]]:
    return [(s.start_char, s.end_char) for s in parse_sections(text)]


class TestCnNumerals:
    def test_conversions(self):
        assert _cn_to_int("三") == 3
        assert _cn_to_int("十") == 10
        assert _cn_to_int("十五") == 15
        assert _cn_to_int("二十三") == 23
        assert _cn_to_int("三十") == 30
        assert _cn_to_int("垃圾") is None

    def test_selector_variants(self):
        assert _selector_to_index("3") == 3
        assert _selector_to_index("3.") == 3
        assert _selector_to_index("第3节") == 3
        assert _selector_to_index("三") == 3
        assert _selector_to_index("第三章") == 3
        assert _selector_to_index("标题文本") is None


class TestAtxHeadings:
    def test_nesting_levels(self):
        text = "# 第一章\n甲\n## 第一节\n乙\n### （一）\n丙\n## 第二节\n丁"
        sections = parse_sections(text)
        assert [(s.level, s.title) for s in sections] == [
            (1, "第一章"),
            (2, "第一节"),
            (3, "（一）"),
            (2, "第二节"),
        ]

    def test_ranges_close_at_same_or_higher_level(self):
        text = "# A\n正文A\n# B\n正文B"
        starts, ends = zip(*_ranges(text)) if _ranges(text) else ([], [])
        assert starts == (0, text.index("# B"))
        assert ends == (text.index("# B"), len(text))

    def test_level_capped_at_three(self):
        text = "###### deep"
        (section,) = parse_sections(text)
        assert section.level == 3


class TestChineseHeadings:
    def test_official_document_levels(self):
        text = "第一章 总则\n条款\n第二章 主体\n一、职责\n（一）分工\n二、程序"
        assert _titles(text) == ["第一章 总则", "第二章 主体", "一、职责", "（一）分工", "二、程序"]

    def test_article_lines_are_not_headings(self):
        text = "第一章 总则\n第一条 为了……\n第二条 ……"
        sections = parse_sections(text)
        assert [s.title for s in sections] == ["第一章 总则"]
        assert sections[0].chars == len(text) - 0  # runs to end

    def test_mixed_atx_and_chinese(self):
        text = "# 一、背景\n内容\n第二章 方案\n细节"
        assert [(s.level, s.title) for s in parse_sections(text)] == [
            (1, "一、背景"),
            (1, "第二章 方案"),
        ]


class TestDegradation:
    def test_heading_less_text_gets_blocks(self):
        import courtier.agent.artifacts.outline as mod

        text = "无标题内容" * 1500  # ~6000 chars
        sections = parse_sections(text)
        assert len(sections) == 2
        assert [s.title for s in sections] == ["块 1", "块 2"]
        assert sections[0].chars == mod._BLOCK_CHARS
        assert sections[1].chars == len(text) - mod._BLOCK_CHARS

    def test_empty_text(self):
        assert parse_sections("") == []


class TestFindSection:
    TEXT = "# 一、项目背景\n背景正文\n## 建设方案\n方案正文\n## 经费测算\n经费正文"

    def test_exact_title(self):
        (s,) = [s for s in parse_sections(self.TEXT) if s.title == "建设方案"]
        found = find_section(parse_sections(self.TEXT), "建设方案")
        assert found == s

    def test_index_selectors(self):
        sections = parse_sections(self.TEXT)
        assert find_section(sections, "2") is sections[1]
        assert find_section(sections, "三") is sections[2]
        assert find_section(sections, "第三节") is sections[2]

    def test_containment_fallback(self):
        sections = parse_sections(self.TEXT)
        assert find_section(sections, "经费") is sections[2]

    def test_miss_returns_none(self):
        assert find_section(parse_sections(self.TEXT), "不存在的章节") is None
