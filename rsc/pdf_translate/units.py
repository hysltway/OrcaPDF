from __future__ import annotations

import re

import fitz

from .config import MAX_BATCH_CHARS, MAX_BATCH_ITEMS, MAX_UNIT_CHARS
from .extraction import (
    has_ieee_journal_header,
    is_body_block,
    is_caption,
    is_figure_label,
    is_formula_block,
    is_heading,
    is_lettered_section_heading,
    is_reference_block,
    is_roman_section_heading,
    is_table_block,
    is_version_footer,
    split_ieee_drop_cap_heading,
)
from .models import LayoutGroup, PageData, TextBlock, Unit

def translation_batches(
    items: list[tuple[int, str]],
    max_items: int = MAX_BATCH_ITEMS,
    max_chars: int = MAX_BATCH_CHARS,
) -> list[list[tuple[int, str]]]:
    batches = []
    batch = []
    char_count = 0

    for item in items:
        text_len = len(item[1])
        if batch and (len(batch) >= max_items or char_count + text_len > max_chars):
            batches.append(batch)
            batch = []
            char_count = 0

        batch.append(item)
        char_count += text_len

    if batch:
        batches.append(batch)
    return batches


def page_blocks(pages: list[PageData]) -> list[tuple[PageData, TextBlock]]:
    return [(page, block) for page in pages for block in page.blocks]


def paragraph_continues(text: str) -> bool:
    text = text.rstrip()
    if not text:
        return False
    if text.endswith((".", "?", "!", "。", "？", "！", ":", "：")):
        return False
    if text.endswith(";"):
        return True
    return text[-1].islower() or text[-1] in ",，-–"


def can_merge(prev_page: PageData, prev: TextBlock, page: PageData, block: TextBlock) -> bool:
    if (
        is_caption(prev.text)
        or is_caption(block.text)
        or is_heading(prev)
        or is_heading(block)
        or (has_ieee_journal_header(prev_page) and is_roman_section_heading(prev))
        or (has_ieee_journal_header(page) and is_roman_section_heading(block))
        or (has_ieee_journal_header(prev_page) and is_lettered_section_heading(prev))
        or (has_ieee_journal_header(page) and is_lettered_section_heading(block))
        or prev.bold
        or block.bold
    ):
        return False
    if has_ieee_journal_header(page) and block.text.lstrip().startswith("•"):
        return False
    if not paragraph_continues(prev.text):
        return False
    if page.index == prev_page.index:
        prev_rect = fitz.Rect(prev.rect)
        rect = fitz.Rect(block.rect)
        if abs(prev_rect.x0 - rect.x0) < 25:
            return True
        return prev_rect.x0 < page.width / 2 < rect.x0 and rect.y0 < prev_rect.y0
    return page.index == prev_page.index + 1 and fitz.Rect(block.rect).y0 < 120


def breaks_translation_unit(page: PageData, block: TextBlock, references_start: tuple[int, float, int, float] | None) -> bool:
    return (
        is_reference_block(page, block, references_start)
        or is_table_block(block)
        or is_formula_block(block)
        or is_figure_label(page, block)
    )


def translation_units(blocks: list[tuple[PageData, TextBlock]], references_start: tuple[int, float, int, float] | None) -> list[Unit]:
    units = []
    current_indexes = []
    current_texts = []
    previous: tuple[int, PageData, TextBlock] | None = None

    for index, (page, block) in enumerate(blocks):
        if not is_body_block(page, block, references_start):
            if current_indexes and breaks_translation_unit(page, block, references_start):
                units.append(Unit(current_indexes, " ".join(current_texts)))
                current_indexes = []
                current_texts = []
                previous = None
            continue

        if (
            current_indexes
            and previous
            and previous[1].index == page.index
            and has_ieee_journal_header(page)
            and (heading_parts := split_ieee_drop_cap_heading(previous[2].text))
            and abs(fitz.Rect(previous[2].rect).x0 - fitz.Rect(block.rect).x0) <= 24
            and 0 <= fitz.Rect(block.rect).y0 - fitz.Rect(previous[2].rect).y0 <= 40
            and re.match(r"^[A-Z][A-Z-]+", block.text)
        ):
            current_texts[-1] = heading_parts[0]
            current_indexes.append(index)
            current_texts.append(heading_parts[1] + block.text)
            previous = index, page, block
            continue

        merged_text_len = sum(len(text) for text in current_texts) + len(block.text) + len(current_texts)
        if previous is None or not can_merge(previous[1], previous[2], page, block) or merged_text_len > MAX_UNIT_CHARS:
            if current_indexes:
                units.append(Unit(current_indexes, " ".join(current_texts)))
            current_indexes = [index]
            current_texts = [block.text]
        else:
            current_indexes.append(index)
            current_texts.append(block.text)
        previous = index, page, block

    if current_indexes:
        units.append(Unit(current_indexes, " ".join(current_texts)))
    return units


def is_layout_body_block(page: PageData, block: TextBlock, references_start: tuple[int, float, int, float] | None) -> bool:
    rect = fitz.Rect(block.rect)
    return (
        is_body_block(page, block, references_start)
        and not is_caption(block.text)
        and not is_heading(block)
        and block.align == "left"
        and not block.bold
        and not block.italic
        and not block.leading_bold
        and rect.width >= 170
        and 6 <= block.font_size <= 12
    )


def can_merge_layout_block(page: PageData, prev: TextBlock, block: TextBlock) -> bool:
    prev_rect = fitz.Rect(prev.rect)
    rect = fitz.Rect(block.rect)
    gap = rect.y0 - prev_rect.y1
    same_column = (prev_rect.x0 < page.width / 2) == (rect.x0 < page.width / 2)
    return (
        -2 <= gap <= max(28, prev.font_size * 3.0)
        and rect.y0 >= prev_rect.y0 - 2
        and abs(prev_rect.x0 - rect.x0) <= 24
        and abs(prev_rect.width - rect.width) <= 36
        and abs(prev.font_size - block.font_size) <= 1.5
        and same_column
    )


def rendered_translation_text(items: list[tuple[int, TextBlock, str]]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []

    for offset, (_, block, translated) in enumerate(items):
        if offset and not paragraph_continues(items[offset - 1][1].text):
            paragraph = "".join(current).strip()
            if paragraph:
                paragraphs.append(paragraph)
            current = []
        current.append(translated)

    paragraph = "".join(current).strip()
    if paragraph:
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def layout_groups(
    page: PageData,
    translations: list[tuple[int, TextBlock, str]],
    references_start: tuple[int, float, int, float] | None,
) -> list[LayoutGroup]:
    groups = []
    current: list[tuple[int, TextBlock, str]] = []
    headings = [
        fitz.Rect(block.rect)
        for block in page.blocks
        if is_heading(block)
    ]

    def flush_current() -> None:
        nonlocal current
        if current:
            groups.append(LayoutGroup([block for _, block, _ in current], rendered_translation_text(current)))
            current = []

    def crosses_heading(prev: TextBlock, block: TextBlock) -> bool:
        prev_rect = fitz.Rect(prev.rect)
        rect = fitz.Rect(block.rect)
        if abs(prev_rect.x0 - rect.x0) > 24:
            return False
        top = min(prev_rect.y1, rect.y1)
        bottom = max(prev_rect.y0, rect.y0)
        for heading in headings:
            overlaps_column = min(prev_rect.x1, rect.x1, heading.x1) - max(prev_rect.x0, rect.x0, heading.x0) > 20
            if overlaps_column and top < heading.y0 < bottom:
                return True
        return False

    for item in translations:
        block_index, block, _ = item
        if is_version_footer(page, block):
            flush_current()
            groups.append(LayoutGroup([block], "", hidden=True))
            continue
        if not is_layout_body_block(page, block, references_start):
            flush_current()
            if not item[2]:
                groups.append(LayoutGroup([block], "", hidden=True))
                continue
            groups.append(LayoutGroup([block], item[2]))
            continue

        if (
            current
            and block_index == current[-1][0] + 1
            and can_merge_layout_block(page, current[-1][1], block)
            and not crosses_heading(current[-1][1], block)
        ):
            current.append(item)
        else:
            flush_current()
            current = [item]

    flush_current()
    return groups


def split_translation(text: str, blocks: list[TextBlock], line_height: float) -> list[str]:
    if len(blocks) == 1:
        return [text]

    capacities = []
    for block in blocks:
        rect = fitz.Rect(block.rect)
        chars_per_line = max(1, int(rect.width / (block.font_size * 0.95)))
        lines = max(1, int(rect.height / (block.font_size * line_height)))
        capacities.append(chars_per_line * lines)
    total = sum(capacities)
    parts = []
    start = 0
    used_capacity = 0
    for capacity in capacities[:-1]:
        used_capacity += capacity
        target = round(len(text) * used_capacity / total)
        split_at = split_position(text, start, target)
        parts.append(text[start:split_at].strip())
        start = split_at
        while start < len(text) and text[start].isspace():
            start += 1
    parts.append(text[start:].strip())
    return parts


def is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "-_")


def split_position(text: str, start: int, target: int) -> int:
    if target <= start:
        return min(len(text), start + 1)
    if target >= len(text):
        return len(text)

    left = max(start + 1, target - 80)
    right = min(len(text), target + 80)
    for marks in ("。！？；", "，、", " "):
        candidates = [text.rfind(mark, left, right) for mark in marks]
        split_at = max(candidates)
        if split_at >= left:
            return split_at if text[split_at].isspace() else split_at + 1

    split_at = min(max(target, start + 1), len(text) - 1)
    while (
        split_at > start + 1
        and split_at < len(text)
        and is_ascii_word_char(text[split_at - 1])
        and is_ascii_word_char(text[split_at])
    ):
        split_at -= 1
    return split_at
