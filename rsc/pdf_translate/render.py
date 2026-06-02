from __future__ import annotations

import html
import time
from functools import cache
from pathlib import Path

import fitz

from .config import (
    CJK_BOLD_FONT,
    CJK_REGULAR_FONT,
    DEFAULT_TEXT_LINE_HEIGHT,
    FAST_CJK_BOLD_FONT_NAME,
    FAST_CJK_FONT_NAME,
    FONT_DIR,
    FONT_FACE_CSS,
    MIN_FONT_SIZE,
)
from .extraction import body_font_size, find_references_range, is_caption, is_heading, is_version_footer
from .models import LayoutGroup, Logger, PageData, TextBlock
from .units import layout_groups

def is_cjk(char: str) -> bool:
    return (
        "\u3400" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or char in "，。！？；：、（）《》【】“”‘’"
    )


def html_fragments(text: str) -> str:
    parts = []
    current = []
    current_class = None

    def flush_current() -> None:
        nonlocal current, current_class
        if current:
            parts.append(f'<span class="{current_class}">{html.escape("".join(current))}</span>')
            current = []

    for char in text:
        if char == "\n":
            flush_current()
            parts.append("<br>")
            current_class = None
            continue

        char_class = "cjk" if is_cjk(char) else "latin"
        if current and char_class != current_class:
            flush_current()
        current.append(char)
        current_class = char_class

    flush_current()
    return "".join(parts)


def markdown_bold_segments(text: str) -> list[tuple[str, bool]]:
    segments = []
    current = []
    bold = False
    index = 0
    while index < len(text):
        if text.startswith("**", index):
            if current:
                segments.append(("".join(current), bold))
                current = []
            bold = not bold
            index += 2
            continue
        current.append(text[index])
        index += 1

    if current:
        segments.append(("".join(current), bold))
    return segments


def style_class(block: TextBlock) -> str:
    if block.bold and block.italic:
        return "bolditalic"
    if block.bold:
        return "bold"
    if block.italic:
        return "italic"
    return "regular"


def leading_style_end(text: str) -> int:
    space = text.find(" ")
    if 0 < space <= 24:
        return space + 1

    for mark in ("：", ":", "。", "."):
        pos = text.find(mark)
        if 0 < pos <= 28:
            return pos + 1

    comma = text.find("，")
    if 0 < comma <= 12:
        return comma + 1
    return 0


def bold_style(base_style: str) -> str:
    if base_style == "italic":
        return "bolditalic"
    return base_style if base_style in {"bold", "bolditalic"} else "bold"


def styled_text(text: str, base_style: str) -> str:
    return "".join(
        f'<span class="{bold_style(base_style) if bold else base_style}">{html_fragments(segment)}</span>'
        for segment, bold in markdown_bold_segments(text)
        if segment
    )


def styled_html(text: str, block: TextBlock) -> str:
    if block.leading_bold:
        end = leading_style_end(text)
        if end:
            heading = text[:end]
            rest = text[end:]
            return styled_text(heading, "bold") + styled_text(rest, "regular")
    if block.leading_bold and "。" in text:
        heading, rest = text.split("。", 1)
        return styled_text(heading + "。", "bold") + styled_text(rest, "regular")
    return styled_text(text, style_class(block))


@cache
def textbox_css(font_size: float, align: str, line_height: float) -> str:
    return f"""
{FONT_FACE_CSS}
body {{ margin: 0; font-size: {font_size}pt; line-height: {line_height}; text-align: {align}; }}
.cjk {{ font-family: CjkRegularLocal; }}
.latin {{ font-family: TimesLocal; }}
.bold .cjk {{ font-family: CjkBoldLocal; }}
.bold .latin {{ font-family: TimesBoldLocal; }}
.italic .cjk {{ font-family: CjkRegularLocal; font-style: italic; }}
.italic .latin {{ font-family: TimesItalicLocal; }}
.bolditalic .cjk {{ font-family: CjkBoldLocal; font-style: italic; }}
.bolditalic .latin {{ font-family: TimesBoldItalicLocal; }}
"""


def round_font_size(size: float) -> float:
    return round(max(MIN_FONT_SIZE, size) * 2) / 2


def is_body_like_translation_block(rect: fitz.Rect, block: TextBlock) -> bool:
    return (
        block.align == "left"
        and not block.bold
        and not block.italic
        and not is_caption(block.text)
        and block.line_count >= 2
        and rect.width >= 170
        and block.font_size <= 12
    )


def base_font_size(rect: fitz.Rect, block: TextBlock, regular_body_size: float | None) -> float:
    if is_body_like_translation_block(rect, block):
        body_size = regular_body_size or block.font_size
        if body_size >= 9.5:
            body_size -= 1.0
        elif body_size >= 8.5:
            body_size -= 0.5
        return round_font_size(min(body_size, 9.5))
    limit = 18.0 if (block.align == "center" or block.bold or block.line_count <= 2) else 10.0
    return round_font_size(min(block.font_size, limit))


def font_size_candidates(rect: fitz.Rect, block: TextBlock, regular_body_size: float | None) -> list[float]:
    base = base_font_size(rect, block, regular_body_size)
    drops = (0.0, 0.5, 1.0, 1.5, 2.0) if is_body_like_translation_block(rect, block) else (0.0, 0.5, 1.0, 1.5, 2.0)
    sizes = []
    for drop in drops:
        size = round_font_size(base - drop)
        if size not in sizes:
            sizes.append(size)
    return sizes


def layout_candidates(
    rect: fitz.Rect,
    block: TextBlock,
    regular_body_size: float | None,
    line_height: float,
) -> list[tuple[float, float]]:
    sizes = font_size_candidates(rect, block, regular_body_size)
    line_heights = [line_height]
    if is_body_like_translation_block(rect, block):
        for compact_line_height in (1.1, 1.0):
            if compact_line_height < line_height and compact_line_height not in line_heights:
                line_heights.append(compact_line_height)
    return [(size, candidate_line_height) for size in sizes for candidate_line_height in line_heights]


def cover_original_text(page: fitz.Page, blocks: list[TextBlock]) -> int:
    if not blocks:
        return 0
    shape = page.new_shape()
    for block in blocks:
        rect = fitz.Rect(block.rect) + (-0.8, -0.8, 0.8, 0.8)
        shape.draw_rect(rect)
    shape.finish(width=0, color=(1, 1, 1), fill=(1, 1, 1))
    shape.commit(overlay=True)
    return len(blocks)


def text_align_value(align: str) -> int:
    return fitz.TEXT_ALIGN_CENTER if align == "center" else fitz.TEXT_ALIGN_LEFT


def can_use_fast_textbox(block: TextBlock, translated: str) -> bool:
    return not (
        block.italic
        or block.leading_bold
        or "**" in translated
        or any(char in translated for char in "，。！？；：、）】》”’")
    )


def write_fast_translation(
    page: fitz.Page,
    block: TextBlock,
    translated: str,
    candidates: list[tuple[float, float]],
) -> bool:
    rect = fitz.Rect(block.rect)
    align = text_align_value(block.align)
    fontfile = CJK_BOLD_FONT if block.bold else CJK_REGULAR_FONT
    fontname = FAST_CJK_BOLD_FONT_NAME if block.bold else FAST_CJK_FONT_NAME
    for size, line_height in candidates:
        shape = page.new_shape()
        spare_height = shape.insert_textbox(
            rect,
            translated,
            fontfile=fontfile,
            fontname=fontname,
            fontsize=size,
            lineheight=line_height,
            align=align,
        )
        if spare_height >= 0:
            shape.commit(overlay=True)
            return True
    return False


def write_html_translation(
    page: fitz.Page,
    rect: fitz.Rect,
    html_text: str,
    block: TextBlock,
    archive: fitz.Archive,
    candidates: list[tuple[float, float]],
) -> None:
    for size, line_height in candidates:
        spare_height, _ = page.insert_htmlbox(
            rect,
            html_text,
            css=textbox_css(size, block.align, line_height),
            archive=archive,
            scale_low=1,
            overlay=True,
        )
        if spare_height >= 0:
            return

    size, line_height = candidates[-1]
    spare_height, _ = page.insert_htmlbox(
        rect,
        html_text,
        css=textbox_css(size, block.align, line_height),
        archive=archive,
        scale_low=0.9,
        overlay=True,
    )
    if spare_height >= 0:
        return

    page.insert_htmlbox(
        rect,
        html_text,
        css=textbox_css(size, block.align, line_height),
        archive=archive,
        scale_low=0.4,
        overlay=True,
    )


def write_translation(
    page: fitz.Page,
    block: TextBlock,
    translated: str,
    archive: fitz.Archive,
    regular_body_size: float | None,
    line_height: float,
    used_rects: list[fitz.Rect] | None = None,
) -> str:
    rect = fitz.Rect(block.rect)
    if used_rects is not None:
        for used in used_rects:
            overlaps_x = min(rect.x1, used.x1) - max(rect.x0, used.x0) > 20
            overlaps_y = min(rect.y1, used.y1) - max(rect.y0, used.y0) > 1
            if overlaps_x and overlaps_y:
                rect.y0 = max(rect.y0, used.y1 + 1)
        if rect.y0 >= rect.y1 - 2:
            rect = fitz.Rect(block.rect)
    if is_heading(block):
        rect += (-2, -2, 2, max(3, block.font_size * 0.35))
    candidates = layout_candidates(rect, block, regular_body_size, line_height)
    if can_use_fast_textbox(block, translated) and write_fast_translation(page, block, translated, candidates):
        if used_rects is not None:
            used_rects.append(rect)
        return "fast"

    html_text = styled_html(translated, block)
    write_html_translation(page, rect, html_text, block, archive, candidates)
    if used_rects is not None:
        used_rects.append(rect)
    return "html"


def union_rect(blocks: list[TextBlock]) -> fitz.Rect:
    rect = fitz.Rect(blocks[0].rect)
    for block in blocks[1:]:
        rect.include_rect(fitz.Rect(block.rect))
    return rect


def group_block(group: LayoutGroup) -> TextBlock:
    first = group.blocks[0]
    rect = union_rect(group.blocks)
    heading = is_heading(first)
    return TextBlock(
        rect=tuple(rect),
        text=first.text,
        font_size=first.font_size,
        line_count=sum(block.line_count for block in group.blocks),
        bold=first.bold or heading,
        italic=first.italic,
        leading_bold=first.leading_bold,
        align=first.align,
    )


def write_layout_group(
    page: fitz.Page,
    group: LayoutGroup,
    archive: fitz.Archive,
    regular_body_size: float | None,
    line_height: float,
    used_rects: list[fitz.Rect] | None = None,
) -> str:
    if group.hidden:
        return "hidden"
    return write_translation(page, group_block(group), group.text, archive, regular_body_size, line_height, used_rects)


def build_pdf(
    source_doc: fitz.Document,
    pages: list[PageData],
    translations: list[str],
    output_path: Path,
    logger: Logger = None,
    line_height: float = DEFAULT_TEXT_LINE_HEIGHT,
) -> None:
    output = fitz.open()
    output.insert_pdf(source_doc)
    translation_index = 0
    total_blocks = sum(len(page_data.blocks) for page_data in pages)
    processed_blocks = 0
    covered_blocks = 0
    fast_layout_calls = 0
    html_layout_calls = 0
    render_start = time.perf_counter()
    archive = fitz.Archive(FONT_DIR)
    references_start = find_references_range(pages)
    regular_body_size = body_font_size(pages, references_start)

    if logger:
        logger.write(f"  typesetting PDF: checking {total_blocks} extracted text blocks...")
        logger.write(f"  typesetting: line height {line_height:g}")
        if regular_body_size:
            logger.write(f"  typesetting: regular body font size {regular_body_size:g}pt")

    for page_data in pages:
        target_page = output[page_data.index]
        page_translations = []
        used_rects: list[fitz.Rect] = []
        for block_index, block in enumerate(page_data.blocks):
            translated = translations[translation_index]
            translation_index += 1
            if translated == block.text and not is_version_footer(page_data, block):
                continue
            page_translations.append((block_index, block, translated))

        covered_blocks += cover_original_text(target_page, [block for _, block, _ in page_translations])
        for group in layout_groups(page_data, page_translations, references_start):
            layout_mode = write_layout_group(target_page, group, archive, regular_body_size, line_height, used_rects)
            if layout_mode == "fast":
                fast_layout_calls += 1
            elif layout_mode == "html":
                html_layout_calls += 1
            processed_blocks += len(group.blocks)
            if logger and processed_blocks % 10 == 0:
                logger.write(f"    typesetting: rendered {processed_blocks} changed blocks...")

    if logger:
        render_seconds = time.perf_counter() - render_start
        logger.write(
            f"  typesetting: {total_blocks} extracted blocks checked; {covered_blocks} changed source blocks covered and "
            f"{processed_blocks} translated blocks rendered in "
            f"{render_seconds:.1f}s ({fast_layout_calls} fast textbox calls, {html_layout_calls} html layout calls)"
        )
        logger.write("  typesetting: saving finalized PDF output...")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    save_start = time.perf_counter()
    output.save(
        tmp_path,
        garbage=4,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        use_objstms=1,
        compression_effort=6,
    )
    output.close()
    tmp_path.replace(output_path)
    if logger:
        logger.write(f"  typesetting: saved finalized PDF in {time.perf_counter() - save_start:.1f}s")
