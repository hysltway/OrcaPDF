from __future__ import annotations

import re

import fitz

from .models import PageData, TextBlock

def clean_text(text: str) -> str:
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def block_text(lines: list[dict]) -> str:
    parts = []
    for line in lines:
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        if text.strip():
            parts.append(text)
    return clean_text("\n".join(parts))


def line_text(line: dict) -> str:
    return clean_text("".join(span.get("text", "") for span in line.get("spans", [])))


def lines_bbox(lines: list[dict]) -> tuple[float, float, float, float]:
    return (
        min(line["bbox"][0] for line in lines),
        min(line["bbox"][1] for line in lines),
        max(line["bbox"][2] for line in lines),
        max(line["bbox"][3] for line in lines),
    )


def table_header_score(text: str) -> int:
    lower = text.lower()
    patterns = (
        r"\bhyperparameters\b",
        r"\bvalues\b",
        r"\bluna-base\b",
        r"\bluna-large\b",
        r"\bluna-huge\b",
        r"\bstage\b",
        r"\binput shape\b",
        r"\bcomplexity\b",
        r"\bmethod\b",
        r"\bbottleneck\b",
        r"#patches\b",
        r"#channels\b",
        r"\bflops\b",
        r"\bmemory\b",
        r"\bmib\b",
        r"\bdataset\b",
        r"# subjects\b",
        r"# samples\b",
        r"\bhours of recordings\b",
        r"\bmontage\b",
        r"\bvariant\b",
        r"\bauroc\b",
        r"\bauprc\b",
    )
    return sum(re.search(pattern, lower) is not None for pattern in patterns)


def looks_like_table_header(text: str) -> bool:
    if "." in text:
        return False
    return table_header_score(text) >= 2


def split_caption_lines(lines: list[dict]) -> list[list[dict]]:
    if len(lines) <= 1:
        return [lines]

    first = line_text(lines[0])
    rest = block_text(lines[1:])
    if is_caption(first) and looks_like_table_header(rest):
        return [[lines[0]], lines[1:]]
    return [lines]


def split_wrapped_lines(page_width: float, lines: list[dict]) -> list[list[dict]]:
    if len(lines) < 4:
        return [lines]

    rect = fitz.Rect(lines_bbox(lines))
    if rect.width < page_width * 0.55:
        return [lines]

    narrow_x1 = rect.x1 - max(55, rect.width * 0.22)
    groups = []
    current = []
    current_narrow = None

    for line in lines:
        if not line_text(line):
            continue
        is_narrow = line["bbox"][2] < narrow_x1
        if current and is_narrow != current_narrow:
            groups.append(current)
            current = []
        current.append(line)
        current_narrow = is_narrow

    if current:
        groups.append(current)

    if (
        len(groups) > 1
        and fitz.Rect(lines_bbox(groups[0])).width < rect.width - 55
        and len(groups[0]) >= 3
        and len(groups[1]) >= 2
    ):
        return [groups[0], [line for group in groups[1:] for line in group]]
    return [lines]


def trim_caption_blocks(blocks: list[TextBlock]) -> list[TextBlock]:
    trimmed = []
    for block in blocks:
        rect = fitz.Rect(block.rect)
        if is_caption(block.text):
            for next_block in sorted(blocks, key=lambda item: item.rect[1]):
                if next_block is block:
                    continue
                next_rect = fitz.Rect(next_block.rect)
                if next_rect.y0 <= rect.y0:
                    continue
                if next_rect.y0 - rect.y0 > 25:
                    break
                overlaps_x = min(rect.x1, next_rect.x1) - max(rect.x0, next_rect.x0) > 20
                if overlaps_x and looks_like_table_header(next_block.text):
                    rect.y1 = max(rect.y0 + 6, min(rect.y1, next_rect.y0 - 1))
                    break
        trimmed.append(
            TextBlock(
                rect=tuple(rect),
                text=block.text,
                font_size=block.font_size,
                line_count=block.line_count,
                bold=block.bold,
                italic=block.italic,
                leading_bold=block.leading_bold,
                align=block.align,
            )
        )
    return trimmed


def line_count(lines: list[dict]) -> int:
    return sum(
        1
        for line in lines
        if "".join(span.get("text", "") for span in line.get("spans", [])).strip()
    )


def first_font_size(lines: list[dict]) -> float:
    for line in lines:
        for span in line.get("spans", []):
            size = span.get("size")
            if size:
                return float(size)
    return 8.0


def block_align(page_width: float, lines: list[dict], font_size: float) -> str:
    rect = fitz.Rect(lines_bbox(lines))
    if font_size >= 11 and line_count(lines) <= 3 and abs(rect.x0 + rect.x1 - page_width) < 70:
        return "center"
    return "left"


def span_is_bold(span: dict) -> bool:
    font = span.get("font", "").lower()
    return bool(span.get("flags", 0) & 16) or any(token in font for token in ("bold", "medi", "cmbx", "cmmib"))


def span_is_italic(span: dict) -> bool:
    font = span.get("font", "").lower()
    return bool(span.get("flags", 0) & 2) or any(token in font for token in ("italic", "ital", "oblique", "cmmi"))


def block_style(lines: list[dict]) -> tuple[bool, bool, bool]:
    total = 0
    bold = 0
    italic = 0
    leading_bold = False
    found_first = False

    for line in lines:
        for span in line.get("spans", []):
            text_len = len(span.get("text", "").strip())
            if text_len == 0:
                continue
            if not found_first:
                leading_bold = span_is_bold(span)
                found_first = True
            total += text_len
            if span_is_bold(span):
                bold += text_len
            if span_is_italic(span):
                italic += text_len

    if total == 0:
        return False, False, False
    return bold / total >= 0.6, italic / total >= 0.6, leading_bold and bold / total < 0.6


def should_translate(text: str) -> bool:
    if len(text.strip()) < 3:
        return False

    words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", text)
    if not words:
        return False

    symbols = sum(1 for char in text if not char.isalnum() and not char.isspace())
    return symbols / max(len(text), 1) <= 0.45 or len(words) >= 4


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z-]{2,}", text))


def extract_pages(doc: fitz.Document) -> list[PageData]:
    pages = []
    for page_index, page in enumerate(doc):
        blocks = []
        for raw in page.get_text("dict", sort=True)["blocks"]:
            if raw.get("type") != 0:
                continue

            groups = split_caption_lines(raw.get("lines", []))
            for group_index, group in enumerate(groups):
                for lines in split_wrapped_lines(page.rect.width, group):
                    text = block_text(lines)
                    if not text:
                        continue
                    bold, italic, leading_bold = block_style(lines)
                    rect = lines_bbox(lines)
                    if group_index == 0 and len(groups) > 1 and is_caption(text):
                        next_y = min(line["bbox"][1] for line in groups[1])
                        rect = (rect[0], rect[1], rect[2], max(rect[1] + 6, min(rect[3], next_y - 1)))

                    font_size = first_font_size(lines)
                    blocks.append(
                        TextBlock(
                            rect=rect,
                            text=text,
                            font_size=font_size,
                            line_count=line_count(lines),
                            bold=bold,
                            italic=italic,
                            leading_bold=leading_bold,
                            align=block_align(page.rect.width, lines, font_size),
                        )
                    )

        ieee_page = any(is_ieee_journal_header(PageData(page_index, page.rect.width, page.rect.height, blocks), block) for block in blocks)
        blocks.sort(key=lambda block: reading_order_key(page.rect.width, block, ieee_page, page_index))
        blocks = trim_caption_blocks(blocks)
        pages.append(PageData(page_index, page.rect.width, page.rect.height, blocks))
    return pages


def reading_order_key(page_width: float, block: TextBlock, ieee_page: bool = False, page_index: int = 0) -> tuple:
    rect = fitz.Rect(block.rect)
    if rect.y1 < 220 and rect.width > 250:
        return 0, rect.y0, rect.x0
    if ieee_page and page_index > 0 and rect.x0 > page_width / 2 and rect.y0 < 330 and rect.width > 180:
        return 1, rect.y0, rect.x0
    if rect.width > page_width * 0.65:
        return 1, rect.y0, rect.x0
    column = 0 if rect.x0 < page_width / 2 else 1
    return 2, column, rect.y0, rect.x0


def find_references_range(pages: list[PageData]) -> tuple[int, float, int, float] | None:
    start = None
    for page in pages:
        for block in page.blocks:
            if re.match(r"^references\b", block.text, re.IGNORECASE):
                start = (page.index, block.rect[1])
                break
        if start:
            break

    if start is None:
        return None

    start_page, start_y = start
    for page in pages[start_page:]:
        for block in page.blocks:
            if page.index == start_page and block.rect[1] <= start_y:
                continue
            if re.match(
                r"^(?:[A-Z]\s+)?(?:Append(?:ix|ices)|Technical Appendices|Supplementary Material)\b|^NeurIPS Paper Checklist\b",
                block.text,
                re.IGNORECASE,
            ):
                return start_page, start_y, page.index, block.rect[1]

    return start_page, start_y, len(pages), float("inf")


def is_reference_block(page: PageData, block: TextBlock, reference_range: tuple[int, float, int, float] | None) -> bool:
    if reference_range is None:
        return False

    start_page, start_y, end_page, end_y = reference_range
    if page.index < start_page or page.index > end_page:
        return False
    if page.index < start_page:
        return False
    if page.index == start_page:
        rect = fitz.Rect(block.rect)
        if rect.y0 < start_y - 4 and start_y >= 160:
            return False
        start_blocks = [
            fitz.Rect(candidate.rect)
            for candidate in page.blocks
            if re.match(r"^references\b", candidate.text, re.IGNORECASE)
        ]
        if start_blocks:
            start_rect = start_blocks[0]
            if start_y < 160 and start_rect.x0 > page.width / 2:
                return rect.x0 > page.width / 2 and rect.y0 >= start_y - 4
        if rect.y0 < start_y - 4:
            return False
    if page.index == end_page and block.rect[1] >= end_y - 4:
        return False
    return True


def is_table_block(block: TextBlock) -> bool:
    text = block.text.strip()
    number_count = len(re.findall(r"\d+(?:\.\d+)?", text))
    decimal_count = len(re.findall(r"\d+\.\d+", text))
    word_count = len(re.findall(r"[A-Za-z][A-Za-z-]*", text))
    complexity_count = len(re.findall(r"O\(", text))

    if looks_like_table_header(text):
        return True
    if complexity_count >= 2 and word_count <= 60:
        return True
    if text in {"Temporal Encoder"}:
        return True
    if re.match(
        r"^(query self-attention|patch-wise attention encoder|luna \(latent space attention\)|full-attention|alternating attention)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.match(r"^channel-unification module\b", text, re.IGNORECASE) and complexity_count > 0:
        return True
    if re.match(r"^(method|dataset|components)\b", text, re.IGNORECASE) and block.line_count > 1:
        return True
    if "Base-to-Novel" in text and "Few-shot" in text:
        return True
    if "LSCE" in text and "Base" in text and "Novel" in text:
        return True
    if decimal_count >= 3 and number_count * 3 >= word_count:
        return True
    if block.line_count >= 2 and number_count >= 2 and word_count <= 12:
        return True
    if any(mark in text for mark in ("±", "✓", "✗")) and number_count >= 3 and number_count * 2 >= word_count:
        return True
    return block.line_count >= 3 and number_count >= 6 and number_count * 3 >= word_count


def is_formula_block(block: TextBlock) -> bool:
    text = block.text.strip()
    math_chars = sum(1 for char in text if char in "=∈∥⊤⊂∑µκτδϵαθπ−+*/{}[]()")
    if math_chars >= 2 and word_count(text) <= 4:
        return True
    return block.line_count <= 2 and math_chars >= 1 and len(text) < 35


def is_side_note(page: PageData, block: TextBlock) -> bool:
    rect = fitz.Rect(block.rect)
    text = block.text.strip()
    return (
        text.startswith("arXiv:")
        or (rect.x1 < 45 and rect.height > 120)
        or (rect.width < 40 and rect.height > rect.width * 5)
    )


def is_first_page_metadata(page: PageData, block: TextBlock) -> bool:
    if page.index != 0:
        return False
    text = block.text.strip()
    rect = fitz.Rect(block.rect)
    if re.match(r"^abstract\s*[—-]\s*\S", text, re.IGNORECASE):
        return False
    if has_ieee_journal_header(page):
        if rect.x0 > page.width / 2 and rect.y0 >= 180:
            return False
        if text.startswith(("Received ", "Digital Object Identifier", "©")):
            return True
        if re.search(r"\b(?:Member|Fellow|Graduate Student Member),\s+IEEE\b", text):
            return True
        if " with the " in text or " is with the " in text or "source code can be found" in text.lower():
            return True
    if text.startswith(("*", "∗")):
        return True
    if rect.y0 > page.height - 100:
        return True
    if rect.y0 < 130:
        return False
    return rect.y0 < 220


def is_ieee_journal_header(page: PageData, block: TextBlock) -> bool:
    rect = fitz.Rect(block.rect)
    if rect.y0 > 55 or block.font_size > 8.5:
        return False
    text = re.sub(r"\s+", " ", block.text.strip())
    if "IEEE JOURNAL OF" in text.upper():
        return True
    return bool(re.search(r"\bet al\.\s*:", text, re.IGNORECASE)) and re.search(r"\s\d+$", text) is not None


def has_ieee_journal_header(page: PageData) -> bool:
    return any(is_ieee_journal_header(page, block) for block in page.blocks)


def is_version_footer(page: PageData, block: TextBlock) -> bool:
    rect = fitz.Rect(block.rect)
    if rect.y0 < page.height - 65:
        return False
    text = block.text.strip()
    if re.fullmatch(r"\d+", text):
        return False
    return re.search(
        r"\b(?:conference|proceedings|workshop|preprint|under review|neurips|iclr|icml|cvpr|eccv|acl|arxiv)\b",
        text,
        re.IGNORECASE,
    ) is not None


def is_caption(text: str) -> bool:
    return re.search(r"\b(figure|fig\.|table)\s*\d+[:.]", text.strip(), re.IGNORECASE) is not None


def is_figure_label(page: PageData, block: TextBlock) -> bool:
    text = block.text.strip()
    rect = fitz.Rect(block.rect)
    words = re.findall(r"[A-Za-z][A-Za-z-]+", text)
    if re.match(r"^GT\s+\([a-z]\)", text):
        return True
    if page.index > 0 and rect.y1 < 180 and block.font_size <= 8.5 and len(words) <= 8:
        return True
    if bool(re.search(r"\([a-z]\)", text)) and rect.y1 < 540 and word_count(text) <= 20:
        return True
    return len(page.blocks) > 40 and rect.y1 < 430 and rect.width < 220 and len(words) <= 12


def is_heading(block: TextBlock) -> bool:
    text = block.text.strip()
    words = re.findall(r"[A-Za-z][A-Za-z-]*", text)
    numbered = re.match(r"^\d+(?:\.\d+)*\s+(.+)", text)
    if numbered and 1 <= len(words) <= 12 and len(text) <= 120:
        first_word = words[0] if words else ""
        return bool(first_word[:1].isupper())
    if block.font_size < 9:
        return False
    if not block.bold and block.font_size < 11:
        return False
    if not 1 <= len(words) <= 5:
        return False
    if any(char.isdigit() for char in text):
        return False
    if len(text) > 80:
        return False
    return all(word[:1].isupper() for word in words)


def is_lettered_section_heading(block: TextBlock) -> bool:
    text = block.text.strip()
    lettered = re.match(r"^[A-Z]\.\s+(.+)", text)
    if not lettered:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]*", text)
    return block.font_size >= 9 and 1 <= len(words) <= 12 and len(text) <= 120 and lettered.group(1)[:1].isupper()


def is_roman_section_heading(block: TextBlock) -> bool:
    text = re.sub(r"\s+", " ", block.text.strip())
    match = re.match(r"^[IVX]+\.\s+(.+)$", text)
    if not match:
        return False
    heading = match.group(1)
    words = re.findall(r"[A-Za-z][A-Za-z-]*", heading)
    return block.font_size >= 9 and 1 <= len(words) <= 8 and len(text) <= 90 and heading.upper() == heading


def is_title_block(block: TextBlock) -> bool:
    rect = fitz.Rect(block.rect)
    return (
        block.align == "center"
        and block.font_size >= 14
        and block.line_count <= 4
        and rect.width >= 250
        and word_count(block.text) >= 4
    )


def split_ieee_drop_cap_heading(text: str) -> tuple[str, str] | None:
    match = re.match(r"^([IVX]+\.\s+[A-Z][A-Za-z ]+)\s+([A-Z])$", text.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def is_body_block(page: PageData, block: TextBlock, references_start: tuple[int, float, int, float] | None) -> bool:
    rect = fitz.Rect(block.rect)
    if is_ieee_journal_header(page, block):
        return False
    if is_version_footer(page, block):
        return False
    if is_reference_block(page, block, references_start):
        return False
    if is_side_note(page, block):
        return False
    if is_caption(block.text):
        return should_translate(block.text)
    if is_table_block(block):
        return False
    if is_formula_block(block):
        return False
    if is_figure_label(page, block):
        return False
    if is_first_page_metadata(page, block):
        return False
    if page.index == 0 and rect.y0 < 130 and rect.width > 250:
        return should_translate(block.text)
    if has_ieee_journal_header(page) and split_ieee_drop_cap_heading(block.text):
        return rect.width >= 25
    if has_ieee_journal_header(page) and is_roman_section_heading(block):
        return rect.width >= 25
    if has_ieee_journal_header(page) and is_lettered_section_heading(block):
        return rect.width >= 25
    if is_heading(block):
        return rect.width >= 25
    if rect.width < 170:
        return False
    return should_translate(block.text)


def is_regular_body_block(page: PageData, block: TextBlock, references_start: tuple[int, float, int, float] | None) -> bool:
    rect = fitz.Rect(block.rect)
    return (
        is_body_block(page, block, references_start)
        and block.align == "left"
        and not block.bold
        and not block.italic
        and not block.leading_bold
        and block.line_count >= 2
        and rect.width >= 170
        and 6 <= block.font_size <= 12
    )


def body_font_size(pages: list[PageData], references_start: tuple[int, float, int, float] | None) -> float | None:
    sizes = [
        block.font_size
        for page in pages
        for block in page.blocks
        if is_regular_body_block(page, block, references_start)
    ]
    if not sizes:
        return None
    sizes.sort()
    return round(sizes[len(sizes) // 2] * 2) / 2

