"""Monkey-patch for docxnote Paragraph._split_and_mark.

The original implementation does not split XML runs at character boundaries,
causing comments to cover the entire run (often the whole paragraph) instead of
just the target string.  This module replaces it with a correct implementation.
"""

import copy

from docxnote.namespaces import NS
from docxnote.paragraph import Paragraph
from lxml import etree

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _split_run_at(run: etree._Element, offset: int) -> etree._Element | None:
    """Split a ``<w:r>`` element at the given character offset within its text.

    The *run* element is modified in-place so that its text content starts at
    *offset*.  A new ``<w:r>`` element containing the text before *offset* is
    returned.  The caller is responsible for inserting it into the tree.

    Returns ``None`` when *offset* is 0 or past the end (no split needed).
    """

    if offset <= 0:
        return None

    t_elements = run.findall(".//w:t", NS)
    if not t_elements:
        return None

    # Walk through <w:t> elements to locate the split point.
    current_pos = 0
    split_t_idx: int | None = None
    split_local_offset: int | None = None

    for i, t_el in enumerate(t_elements):
        t_len = len(t_el.text) if t_el.text else 0
        if current_pos + t_len > offset:
            split_t_idx = i
            split_local_offset = offset - current_pos
            break
        current_pos += t_len

    if split_t_idx is None:
        return None  # offset at or past end of run text

    # Build the "before" run.
    rPr = run.find("w:rPr", NS)
    before_run = etree.Element(f"{{{NS['w']}}}r")
    if rPr is not None:
        before_run.append(copy.deepcopy(rPr))

    # Copy <w:t> elements that come before the split point.
    for i in range(split_t_idx):
        before_run.append(copy.deepcopy(t_elements[i]))

    # Split the <w:t> element that contains the boundary.
    split_t = t_elements[split_t_idx]
    split_text = split_t.text or ""
    before_text = split_text[:split_local_offset]
    after_text = split_text[split_local_offset:]

    before_t = etree.Element(f"{{{NS['w']}}}t")
    before_t.text = before_text
    before_t.set(_XML_SPACE, "preserve")
    before_run.append(before_t)

    # Modify the original <w:t> in-place.
    split_t.text = after_text
    split_t.set(_XML_SPACE, "preserve")

    # Remove <w:t> elements that moved to the before-run from the original.
    for i in range(split_t_idx):
        run.remove(t_elements[i])

    return before_run


def _split_and_mark(  # noqa: PLR0912 – complexity is inherent to XML manipulation
    self,
    run_positions: list[tuple],
    start_idx: int,
    end_idx: int,
    start: int,
    end: int,
    comment_id: int,
) -> None:
    """Split runs and insert comment markers at precise character positions."""
    parent = self._element

    start_run, start_run_start, _, start_run_text = run_positions[start_idx]
    end_run, end_run_start, _, end_run_text = run_positions[end_idx]

    start_offset = start - start_run_start
    end_offset = end - end_run_start

    same_run = start_idx == end_idx

    # --- Split runs at character boundaries ----------------------------------

    if same_run:
        # May need to split the single run into up to 3 parts.
        before_start: etree._Element | None = None
        before_end: etree._Element | None = None

        if start_offset > 0:
            before_start = _split_run_at(start_run, start_offset)
            if before_start is not None:
                idx = list(parent).index(start_run)
                parent.insert(idx, before_start)

        if end_offset < len(end_run_text):
            # After the start-split the run text has shifted; adjust offset.
            adjusted = end_offset - start_offset if start_offset > 0 else end_offset
            before_end = _split_run_at(end_run, adjusted)
            if before_end is not None:
                idx = list(parent).index(end_run)
                parent.insert(idx, before_end)

        # Determine which run elements are inside the comment range.
        # After splitting, the same element (start_run == end_run) was
        # modified in-place, so we must choose the *split-off* run that
        # actually holds the comment text.
        if before_end is not None:
            # End-split happened: before_end holds the comment text.
            first_in_range = before_end
            last_in_range = before_end
        elif before_start is not None:
            # Only start-split: start_run holds text from start to end of run.
            first_in_range = start_run
            last_in_range = start_run
        else:
            # No splits: entire run is the comment.
            first_in_range = start_run
            last_in_range = start_run
    else:
        if start_offset > 0:
            before_start = _split_run_at(start_run, start_offset)
            if before_start is not None:
                idx = list(parent).index(start_run)
                parent.insert(idx, before_start)
        first_in_range = start_run

        if end_offset < len(end_run_text):
            before_end = _split_run_at(end_run, end_offset)
            if before_end is not None:
                idx = list(parent).index(end_run)
                parent.insert(idx, before_end)
                last_in_range = before_end
            else:
                last_in_range = end_run
        else:
            last_in_range = end_run

    # --- Insert comment markers ----------------------------------------------

    try:
        first_idx = list(parent).index(first_in_range)
    except ValueError:
        return

    comment_start_el = etree.Element(
        f"{{{NS['w']}}}commentRangeStart",
        attrib={f"{{{NS['w']}}}id": str(comment_id)},
    )
    parent.insert(first_idx, comment_start_el)

    try:
        last_idx = list(parent).index(last_in_range)
    except ValueError:
        return

    comment_end_el = etree.Element(
        f"{{{NS['w']}}}commentRangeEnd",
        attrib={f"{{{NS['w']}}}id": str(comment_id)},
    )
    comment_ref_run = etree.Element(f"{{{NS['w']}}}r")
    etree.SubElement(
        comment_ref_run,
        f"{{{NS['w']}}}commentReference",
        attrib={f"{{{NS['w']}}}id": str(comment_id)},
    )

    parent.insert(last_idx + 1, comment_end_el)
    parent.insert(last_idx + 2, comment_ref_run)


# Apply the monkey-patch when this module is imported.
# Guard: verify the target method exists with the expected signature.
if not hasattr(Paragraph, "_split_and_mark"):
    raise ImportError(
        f"docxnote {getattr(Paragraph, '__module__', 'unknown')} "
        "does not expose Paragraph._split_and_mark — "
        "the monkey-patch in docannot._patch is incompatible with this version."
    )
Paragraph._split_and_mark = _split_and_mark  # type: ignore[assignment]
