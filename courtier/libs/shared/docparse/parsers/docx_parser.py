from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from lxml import etree

from docmodels import (
    Document,
    Margin,
    Page,
    PageContent,
)

from .base import ParserConfig

logger = logging.getLogger(__name__)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Theme attribute name → font scheme category
_THEME_ATTR_TO_SCHEME = {
    "majorHAnsi": "majorFont",
    "majorEastAsia": "majorFont",
    "minorHAnsi": "minorFont",
    "minorEastAsia": "minorFont",
    "majorBidi": "majorFont",
    "minorBidi": "minorFont",
}


class DocxParser:
    """Parser for DOCX files using python-docx + rule engine."""

    def supports(self, file_path: str) -> bool:
        """Check if file is a DOCX."""
        return Path(file_path).suffix.lower() == ".docx"

    def parse(self, file_path: str, config: ParserConfig | None = None) -> Document:
        """Parse a DOCX file into the Document model.

        Uses rule engine exclusively for structure classification.

        Args:
            file_path: Path to the DOCX file.
            config: Parser configuration.

        Returns:
            Parsed Document model.

        Raises:
            FileNotFoundError: If file_path does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        effective_config = config or ParserConfig.from_env()

        doc = DocxDocument(file_path)
        file_bytes = path.read_bytes()
        doc_id = hashlib.sha256(file_bytes).hexdigest()

        margin = _extract_margin(doc)
        theme_fonts = _resolve_theme_fonts(doc)
        font_resolver = _FontResolver(doc, theme_fonts)
        extracted_lines = _extract_all_lines(doc, font_resolver)

        if not extracted_lines:
            pages = [
                Page(
                    raw=file_bytes,
                    page_content=PageContent(margin=margin),
                    save_path=str(path.absolute()),
                    page_no=0,
                )
            ]
        else:
            page_line_groups = _split_lines_by_page_breaks(extracted_lines)

            has_explicit_breaks = len(page_line_groups) > 1
            if not has_explicit_breaks:
                page_content = _classify_page_lines(
                    extracted_lines, margin, effective_config
                )
                pages = [
                    Page(
                        raw=file_bytes,
                        page_content=page_content,
                        save_path=str(path.absolute()),
                        page_no=0,
                    )
                ]
            else:
                pages = []
                for page_idx, page_lines in enumerate(page_line_groups):
                    page_content = _classify_page_lines(
                        page_lines, margin, effective_config
                    )
                    pages.append(
                        Page(
                            raw=file_bytes,
                            page_content=page_content,
                            save_path=str(path.absolute()),
                            page_no=page_idx,
                        )
                    )

        return Document(
            doc_id=doc_id,
            total_page_num=len(pages),
            save_path=str(path.absolute()),
            pages=pages,
        )


def _extract_margin(doc: DocxDocument) -> Margin:
    """Extract margins from the first section of the DOCX document.

    python-docx section margins are Length objects (EMU-based) with
    built-in .mm property for direct millimeter conversion.
    """
    section = doc.sections[0] if doc.sections else None
    if not section:
        return Margin()

    return Margin(
        top_margin=(round(section.top_margin.mm, 2) if section.top_margin else 0.0),
        bottom_margin=(
            round(section.bottom_margin.mm, 2) if section.bottom_margin else 0.0
        ),
        left_margin=(round(section.left_margin.mm, 2) if section.left_margin else 0.0),
        right_margin=(
            round(section.right_margin.mm, 2) if section.right_margin else 0.0
        ),
    )


def _has_page_break(para: Any) -> bool:
    """Check if a paragraph contains an explicit page break."""
    for br in para._element.iter(f"{{{_W_NS}}}br"):
        br_type = br.get(f"{{{_W_NS}}}type")
        if br_type == "page":
            return True
    return False


def _split_lines_by_page_breaks(
    lines: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split lines into page groups by _page_break markers.

    Returns a list of line lists, one per page. If no page breaks
    are found, returns a single list containing all lines.
    """
    pages: list[list[dict[str, Any]]] = [[]]
    for line in lines:
        if line.get("_page_break"):
            pages.append([])
        else:
            pages[-1].append(line)
    return pages


def _infer_logical_pages(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Infer logical pages from content when no explicit page breaks exist.

    Uses split_docx_regions to identify header/body/footer regions,
    then maps them to logical pages:
    - Header lines → page 0 (first page)
    - Body lines → page 1 (middle page)
    - Footer lines → page 2 (last page)

    If only header+body or body+footer exist, returns 2 pages.
    If only body exists, returns 1 page.
    """
    from .page_regions import split_docx_regions

    if not lines:
        return [[]]

    header_lines, body_lines, footer_lines = split_docx_regions(lines)

    pages: list[list[dict[str, Any]]] = []

    if header_lines:
        pages.append(header_lines)

    if body_lines:
        pages.append(body_lines)

    if footer_lines:
        pages.append(footer_lines)

    if not pages:
        pages = [lines]

    return pages


def _classify_page_lines(
    lines: list[dict[str, Any]],
    margin: Margin,
    config: ParserConfig,
) -> PageContent:
    """Classify lines of a single page into PageContent.

    Uses rule engine first, falls back to text-only LLM.
    """
    if not lines:
        return PageContent(margin=margin)

    from .rules import StructureRuleEngine
    from .structure_recognizer import _classified_lines_to_page_content

    engine = StructureRuleEngine()
    result = engine.classify_lines(lines, has_position=False)
    return _classified_lines_to_page_content(result.lines, lines, margin)


def _group_runs_by_formatting(
    runs: list[Any],
    para_bold: bool,
    para_italic: bool,
) -> list[tuple[list[Any], bool, bool]]:
    """Group consecutive runs that share the same effective bold/italic."""
    run_groups: list[tuple[list[Any], bool, bool]] = []
    cur_runs = [runs[0]]
    cur_bold = runs[0].font.bold if runs[0].font.bold is not None else para_bold
    cur_italic = runs[0].font.italic if runs[0].font.italic is not None else para_italic

    for run in runs[1:]:
        r_bold = run.font.bold if run.font.bold is not None else para_bold
        r_italic = run.font.italic if run.font.italic is not None else para_italic
        if r_bold == cur_bold and r_italic == cur_italic:
            cur_runs.append(run)
        else:
            run_groups.append((cur_runs, cur_bold, cur_italic))
            cur_runs = [run]
            cur_bold = r_bold
            cur_italic = r_italic
    run_groups.append((cur_runs, cur_bold, cur_italic))
    return run_groups


def _extract_paragraph_lines(
    para: Any,
    font_resolver: _FontResolver,
    line_no: int,
) -> tuple[list[dict[str, Any]], int]:
    """Extract line entries for a single DOCX paragraph.

    Returns a tuple of (line_dicts, updated_line_no).
    """
    lines: list[dict[str, Any]] = []
    text = para.text.strip()
    if not text:
        return lines, line_no

    runs = para.runs
    alignment = _resolve_alignment(para)
    style_name = _resolve_style_name(para)

    if not runs:
        font_family = font_resolver.resolve_font_family_from_style(para.style)
        font_size = font_resolver.resolve_font_size(para)
        lines.append(
            {
                "text": text,
                "line_no": line_no,
                "x0": 0.0,
                "y0": 0.0,
                "x1": 0.0,
                "y1": 0.0,
                "font_family": font_family,
                "font_size": font_size,
                "font_weight": False,
                "font_style": False,
                "alignment": alignment,
                "style_name": style_name,
            }
        )
        line_no += 1
        if _has_page_break(para):
            lines.append({"_page_break": True, "line_no": line_no})
            line_no += 1
        return lines, line_no

    # Resolve paragraph-level bold/italic for runs that inherit from style
    para_bold = font_resolver.resolve_font_weight(para)
    para_italic = font_resolver.resolve_font_style(para)

    run_groups = _group_runs_by_formatting(runs, para_bold, para_italic)

    for group_runs, bold, italic in run_groups:
        group_text = "".join(r.text for r in group_runs).strip()
        if not group_text:
            continue

        first_run = group_runs[0]
        font_family = font_resolver.resolve_font_family(first_run, para)
        if first_run.font.size:
            font_size = round(first_run.font.size.pt, 1)
        else:
            font_size = _get_szcs_from_element(first_run._element)

        if not font_family:
            font_family = font_resolver.resolve_font_family_from_style(
                para.style if para.style else None
            )
        if not font_size:
            font_size = font_resolver.resolve_font_size(para)

        lines.append(
            {
                "text": group_text,
                "line_no": line_no,
                "x0": 0.0,
                "y0": 0.0,
                "x1": 0.0,
                "y1": 0.0,
                "font_family": font_family,
                "font_size": font_size,
                "font_weight": bold,
                "font_style": italic,
                "alignment": alignment,
                "style_name": style_name,
            }
        )
        line_no += 1

    if _has_page_break(para):
        lines.append({"_page_break": True, "line_no": line_no})
        line_no += 1

    return lines, line_no


def _extract_all_lines(
    doc: DocxDocument,
    font_resolver: _FontResolver,
) -> list[dict[str, Any]]:
    """Extract all text lines with formatting from a DOCX document.

    Args:
        doc: python-docx Document object.
        font_resolver: Resolves font names through style inheritance.

    Returns:
        List of line dicts with text and font metadata.
    """
    lines: list[dict[str, Any]] = []
    line_no = 0

    for para in doc.paragraphs:
        para_lines, line_no = _extract_paragraph_lines(para, font_resolver, line_no)
        lines.extend(para_lines)

    # Also extract text from table cells, which doc.paragraphs skips
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    para_lines, line_no = _extract_paragraph_lines(
                        para, font_resolver, line_no
                    )
                    lines.extend(para_lines)

    return lines


def _resolve_alignment(para: Any) -> str:
    """Resolve paragraph alignment with style inheritance.

    Walks: paragraph pPr → paragraph style → base style chain.
    Returns one of: "left", "center", "right", "justify".
    """
    # 1. Direct paragraph-level jc element
    pPr = para._element.find(f"{{{_W_NS}}}pPr")
    if pPr is not None:
        jc = pPr.find(f"{{{_W_NS}}}jc")
        if jc is not None:
            val = jc.get(f"{{{_W_NS}}}val")
            if val:
                return _normalize_alignment(val)

    # 2. Style inheritance chain
    style = para.style
    visited: set[str] = set()
    while style is not None:
        if style.style_id in visited:
            break
        visited.add(style.style_id)
        spPr = style.element.find(f"{{{_W_NS}}}pPr")
        if spPr is not None:
            sjc = spPr.find(f"{{{_W_NS}}}jc")
            if sjc is not None:
                val = sjc.get(f"{{{_W_NS}}}val")
                if val:
                    return _normalize_alignment(val)
        style = style.base_style

    return "left"


def _normalize_alignment(val: str) -> str:
    """Normalize OOXML jc values to standard alignment names."""
    mapping = {
        "left": "left",
        "center": "center",
        "right": "right",
        "both": "justify",
        "distribute": "justify",
    }
    return mapping.get(val, "left")


def _resolve_style_name(para: Any) -> str:
    """Resolve the effective paragraph style name.

    Returns the Word built-in style name (e.g. "Heading 1", "Heading 2")
    or the custom style name. Walks the style inheritance chain to find
    the first named style. Returns empty string if no style found.
    """
    style = para.style
    visited: set[str] = set()
    while style is not None:
        if style.style_id in visited:
            break
        visited.add(style.style_id)
        if style.name:
            return style.name
        style = style.base_style
    return ""


class _FontResolver:
    """Resolves effective font family for a DOCX run by walking the
    formatting inheritance chain: run → paragraph style → base style
    chain → document defaults (including theme fonts).
    """

    def __init__(
        self,
        doc: DocxDocument,
        theme_fonts: dict[str, str],
    ) -> None:
        self._doc = doc
        self._theme_fonts = theme_fonts

    def _walk_style_chain(self, style: Any, getter, default):
        """Walk the style inheritance chain, returning the first non-None value.

        Each style in the chain is passed to *getter(style)*. The first
        non-None return value wins. Returns *default* when the chain is
        exhausted or *style* is None.

        Cycle detection is provided via a visited-set of style_ids.
        """
        if style is None:
            return default
        visited: set[str] = set()
        current = style
        while current is not None:
            if current.style_id in visited:
                break
            visited.add(current.style_id)
            result = getter(current)
            if result is not None:
                return result
            current = current.base_style
        return default

    def resolve_font_family(self, run: Any, para: Any) -> str:
        """Resolve the effective font family for a run.

        Walks inheritance: run rPr → paragraph style → base styles →
        doc defaults → theme.
        """
        # 1. Run-level direct font name
        if run.font.name:
            return run.font.name

        # 2. Run-level rFonts (may have theme references)
        rpr_font = self._read_rpr_font(run._element)
        if rpr_font:
            return rpr_font

        # 3. Fall through to style chain
        return self.resolve_font_family_from_style(para.style)

    def resolve_font_family_from_style(self, style: Any) -> str:
        """Walk style inheritance chain to find font family."""

        def getter(s):
            # Check style's font.name (python-docx resolves rFonts ascii attr)
            if s.font.name:
                return s.font.name
            # Check rFonts eastAsia directly (python-docx skips this)
            return _get_rfonts_from_style(s, self._theme_fonts) or None

        return self._walk_style_chain(style, getter, "")

    def resolve_font_size(self, para: Any) -> float:
        """Walk style inheritance chain to find font size in points."""

        def getter(s):
            if s.font.size:
                return round(s.font.size.pt, 1)
            # python-docx only reads w:sz; also check w:szCs for CJK docs
            return _get_szcs_from_style(s) or None

        return self._walk_style_chain(para.style if para.style else None, getter, 0.0)

    def resolve_font_weight(self, para: Any) -> bool:
        """Walk style inheritance chain to find bold setting."""

        def getter(s):
            if s.font.bold is not None:
                return s.font.bold is True
            return None

        return self._walk_style_chain(para.style if para.style else None, getter, False)

    def resolve_font_style(self, para: Any) -> bool:
        """Walk style inheritance chain to find italic setting."""

        def getter(s):
            if s.font.italic is not None:
                return s.font.italic is True
            return None

        return self._walk_style_chain(para.style if para.style else None, getter, False)

    def _read_rpr_font(self, run_element: Any) -> str:
        """Read font name from a run's rPr/w:rFonts element.

        Handles both direct font names and theme references.
        """
        rpr = run_element.find(f"{{{_W_NS}}}rPr")
        if rpr is None:
            return ""
        rfonts = rpr.find(f"{{{_W_NS}}}rFonts")
        if rfonts is None:
            return ""

        # Direct eastAsia font name takes priority for Chinese docs
        ea = rfonts.get(f"{{{_W_NS}}}eastAsia")
        if ea:
            return ea
        ascii_font = rfonts.get(f"{{{_W_NS}}}ascii")
        if ascii_font:
            return ascii_font

        # Try theme references
        for theme_key in ("eastAsiaTheme", "asciiTheme", "hAnsiTheme"):
            theme_val = rfonts.get(f"{{{_W_NS}}}{theme_key}")
            if theme_val:
                resolved = self._theme_fonts.get(theme_val, "")
                if resolved:
                    return resolved

        return ""


def _get_rfonts_from_style(
    style: Any, theme_fonts: dict[str, str] | None = None
) -> str:
    """Get eastAsia or ascii font from a style's rPr element directly.

    Also resolves theme references (e.g. minorEastAsia) via theme_fonts.
    """
    rpr = style.element.find(f"{{{_W_NS}}}rPr")
    if rpr is None:
        return ""
    rfonts = rpr.find(f"{{{_W_NS}}}rFonts")
    if rfonts is None:
        return ""
    # Prefer eastAsia for Chinese documents
    ea = rfonts.get(f"{{{_W_NS}}}eastAsia")
    if ea:
        return ea
    ascii_font = rfonts.get(f"{{{_W_NS}}}ascii")
    if ascii_font:
        return ascii_font
    # Resolve theme references
    if theme_fonts:
        for theme_key in ("eastAsiaTheme", "asciiTheme", "hAnsiTheme", "cstheme"):
            theme_val = rfonts.get(f"{{{_W_NS}}}{theme_key}")
            if theme_val:
                resolved = theme_fonts.get(theme_val, "")
                if resolved:
                    return resolved
    return ""


def _get_szcs_from_element(element: Any) -> float:
    """Read font size from an element's rPr/w:szCs (half-points → points)."""
    rpr = element.find(f"{{{_W_NS}}}rPr")
    if rpr is None:
        return 0.0
    sz_cs = rpr.find(f"{{{_W_NS}}}szCs")
    if sz_cs is None:
        return 0.0
    val = sz_cs.get(f"{{{_W_NS}}}val")
    if val:
        try:
            return round(int(val) / 2, 1)
        except (ValueError, TypeError):
            pass
    return 0.0


def _get_szcs_from_style(style: Any) -> float:
    """Read font size from a style's rPr/w:szCs element (half-points → points)."""
    return _get_szcs_from_element(style.element)


def _resolve_theme_fonts(doc: DocxDocument) -> dict[str, str]:
    """Resolve theme font references from the document's theme part.

    Returns a dict mapping theme attribute names (e.g. 'minorEastAsia')
    to the actual font name (e.g. '等线').
    """
    result: dict[str, str] = {}

    # Find theme part via document relationships
    theme_part = None
    for rel in doc.part.rels.values():
        if "theme" in str(rel.reltype).lower():
            theme_part = rel.target_part
            break

    if theme_part is None:
        return result

    try:
        root = etree.fromstring(theme_part.blob)
    except Exception:
        return result

    scheme = root.find(f"{{{_A_NS}}}themeElements/{{{_A_NS}}}fontScheme")
    if scheme is None:
        return result

    scheme_map = {
        "majorFont": scheme.find(f"{{{_A_NS}}}majorFont"),
        "minorFont": scheme.find(f"{{{_A_NS}}}minorFont"),
    }

    # For each scheme, resolve Hans (Simplified Chinese) → eastAsia → latin
    for scheme_name, scheme_elem in scheme_map.items():
        if scheme_elem is None:
            continue

        # Priority: Hans script > eastAsia element > latin element
        typeface = ""
        hans = scheme_elem.find(f"{{{_A_NS}}}font[@script='Hans']")
        if hans is not None and hans.get("typeface"):
            typeface = hans.get("typeface")
        else:
            ea = scheme_elem.find(f"{{{_A_NS}}}ea")
            if ea is not None and ea.get("typeface"):
                typeface = ea.get("typeface")
            else:
                lat = scheme_elem.find(f"{{{_A_NS}}}latin")
                if lat is not None:
                    typeface = lat.get("typeface", "")

        if typeface:
            for attr_key, cat in _THEME_ATTR_TO_SCHEME.items():
                if cat == scheme_name:
                    result[attr_key] = typeface

    return result
