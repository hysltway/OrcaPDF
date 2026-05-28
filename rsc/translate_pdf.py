from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import fitz
import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "original_PDF"
OUTPUT_DIR = ROOT / "processed_PDF"
LOG_PATH = ROOT / "_translate_log.txt"

SILICONFLOW_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_MODEL = "tencent/Hunyuan-MT-7B"
TRANSLATE_API_KEY_ENV = "siliconflow_TRANSLATE_API_KEY"
TYPESETTING_LINE_HEIGHT_ENV = "PDF_TRANSLATE_LINE_HEIGHT"
TARGET_LANGUAGE = "zh-CN"
CJK_REGULAR_FONT = "C:/Windows/Fonts/STSONG.TTF"
CJK_BOLD_FONT = CJK_REGULAR_FONT
TIMES_FONT = "C:/Windows/Fonts/times.ttf"
TIMES_BOLD_FONT = "C:/Windows/Fonts/timesbd.ttf"
TIMES_ITALIC_FONT = "C:/Windows/Fonts/timesi.ttf"
TIMES_BOLD_ITALIC_FONT = "C:/Windows/Fonts/timesbi.ttf"
FONT_DIR = str(Path(CJK_REGULAR_FONT).parent)
FONT_FACE_CSS = f"""
@font-face {{ font-family: CjkRegularLocal; src: url({Path(CJK_REGULAR_FONT).name}); }}
@font-face {{ font-family: CjkBoldLocal; src: url({Path(CJK_BOLD_FONT).name}); }}
@font-face {{ font-family: TimesLocal; src: url({Path(TIMES_FONT).name}); }}
@font-face {{ font-family: TimesBoldLocal; src: url({Path(TIMES_BOLD_FONT).name}); }}
@font-face {{ font-family: TimesItalicLocal; src: url({Path(TIMES_ITALIC_FONT).name}); }}
@font-face {{ font-family: TimesBoldItalicLocal; src: url({Path(TIMES_BOLD_ITALIC_FONT).name}); }}
"""
MIN_FONT_SIZE = 4.5  # 翻译回写 PDF 时的最小字号限制（防止文字缩得过小无法阅读）
DEFAULT_TEXT_LINE_HEIGHT = 1.2  # 排版时默认的文本行高
FAST_CJK_FONT_NAME = "CjkRegularFast"
MAX_CONCURRENT_BATCHES = 128  # 允许并发提交到大模型翻译的批次（Batch）上限
MAX_BATCH_ITEMS = 1  # 每个翻译批次中包含的文本单元（Unit）数量最大值
MAX_BATCH_CHARS = 12000  # 每个翻译批次所允许的最大字符长度限制
MAX_UNIT_CHARS = 2500  # 单个合并文本单元（Unit）的最大字符长度上限
MAX_CONCURRENT_PDFS = 4  # 允许同时并发处理的 PDF 文件任务上限
TRANSLATION_ATTEMPTS = 3  # 单个翻译请求失败后的最大重试次数
TRANSLATION_TIMEOUT = 60  # 单个翻译请求的超时时间（秒）
RETRY_SLEEP_SECONDS = 0.5  # 翻译失败重试前的休眠等待时间（秒）

_llm_request_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_BATCHES)
_thread_state = threading.local()


@dataclass(slots=True)
class TextBlock:
    rect: tuple[float, float, float, float]
    text: str
    font_size: float
    line_count: int
    bold: bool
    italic: bool
    leading_bold: bool
    align: str


@dataclass(slots=True)
class PageData:
    index: int
    width: float
    height: float
    blocks: list[TextBlock]


class Logger:
    def __init__(self, path: Path, progress_callback=None) -> None:
        self.file = path.open("w", encoding="utf-8")
        self.progress_callback = progress_callback
        self.lock = threading.Lock()

    def write(self, message: str = "") -> None:
        with self.lock:
            self.file.write(message + "\n")
            self.file.flush()
            try:
                print(message)
            except UnicodeEncodeError:
                print(message.encode("ascii", errors="replace").decode("ascii"))
            if self.progress_callback and hasattr(self.progress_callback, "on_log"):
                self.progress_callback.on_log(message)

    def close(self) -> None:
        with self.lock:
            self.file.close()


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

        blocks.sort(key=lambda block: reading_order_key(page.rect.width, block))
        blocks = trim_caption_blocks(blocks)
        pages.append(PageData(page_index, page.rect.width, page.rect.height, blocks))
    return pages


def reading_order_key(page_width: float, block: TextBlock) -> tuple:
    rect = fitz.Rect(block.rect)
    if rect.y1 < 220 and rect.width > 250:
        return 0, rect.y0, rect.x0
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
    if page.index == start_page and block.rect[1] < start_y - 4:
        rect = fitz.Rect(block.rect)
        return start_y < 160 and rect.x0 > page.width / 2
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
    if block.text.startswith(("*", "∗")):
        return True
    if block.rect[1] > page.height - 100:
        return True
    if block.rect[1] < 130:
        return False
    return block.rect[1] < 220


def is_caption(text: str) -> bool:
    return re.search(r"\b(figure|fig\.|table)\s*\d+[:.]", text.strip(), re.IGNORECASE) is not None


def is_figure_label(page: PageData, block: TextBlock) -> bool:
    text = block.text.strip()
    rect = fitz.Rect(block.rect)
    words = re.findall(r"[A-Za-z][A-Za-z-]+", text)
    if re.match(r"^GT\s+\([a-z]\)", text):
        return True
    if bool(re.search(r"\([a-z]\)", text)) and rect.y1 < 540 and word_count(text) <= 20:
        return True
    return len(page.blocks) > 40 and rect.y1 < 430 and rect.width < 220 and len(words) <= 12


def is_heading(block: TextBlock) -> bool:
    text = block.text.strip()
    words = re.findall(r"[A-Za-z][A-Za-z-]*", text)
    if re.match(r"^\d+(?:\.\d+)*\s+\S", text) and 1 <= len(words) <= 12 and len(text) <= 120:
        return True
    if not 1 <= len(words) <= 5:
        return False
    if any(char.isdigit() for char in text):
        return False
    if len(text) > 80:
        return False
    return all(word[:1].isupper() for word in words)


def is_body_block(page: PageData, block: TextBlock, references_start: tuple[int, float, int, float] | None) -> bool:
    rect = fitz.Rect(block.rect)
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
    if is_heading(block):
        return rect.width >= 55
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


@dataclass(slots=True)
class Unit:
    indexes: list[int]
    text: str


@dataclass(slots=True)
class LayoutGroup:
    blocks: list[TextBlock]
    text: str


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
    if is_caption(prev.text) or is_caption(block.text) or is_heading(prev) or is_heading(block) or prev.bold or block.bold:
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


def layout_separator(prev: TextBlock) -> str:
    return "" if paragraph_continues(prev.text) else "\n\n"


def layout_group_text(items: list[tuple[int, TextBlock, str]]) -> str:
    parts = [items[0][2]]
    for offset, (_, block, translated) in enumerate(items[1:], start=1):
        parts.append(layout_separator(items[offset - 1][1]))
        parts.append(translated)
    return "".join(parts)


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
            groups.append(LayoutGroup([block for _, block, _ in current], layout_group_text(current)))
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
        if not is_layout_body_block(page, block, references_start):
            flush_current()
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


def translation_prompt(batch: list[tuple[int, str]]) -> str:
    return json.dumps(
        [{"id": unit_index, "text": text} for unit_index, text in batch],
        ensure_ascii=False,
    )


def parse_translation_response(text: str) -> dict[int, str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    payload = json.loads(text)
    translations = payload["translations"] if isinstance(payload, dict) else payload
    return {
        int(item["id"]): clean_translation(str(item["text"]))
        for item in translations
    }


def thread_session() -> requests.Session:
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        _thread_state.session = session
    return session


def translate_batch(batch_index: int, batch: list[tuple[int, str]], api_key: str) -> tuple[int, list[tuple[int, str]]]:
    if len(batch) == 1:
        unit_index, text = batch[0]
        return batch_index, [(unit_index, translate_unit_text(unit_index, text, api_key))]

    total_chars = sum(len(text) for _, text in batch)
    payload = {
        "model": SILICONFLOW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are translating academic PDF text from English into Simplified Chinese. "
                    "Use precise, fluent academic Chinese. Keep the original meaning complete and do not summarize. "
                    "Preserve terminology consistency, proper nouns, model names, dataset names, citations, numbers, "
                    "units, formulas, inline variables, punctuation that belongs to equations, and bracketed references. "
                    "Return only valid JSON in this exact shape: "
                    "{\"translations\":[{\"id\":number,\"text\":\"translated Chinese\"}]}. "
                    "Return every input id exactly once. Do not translate JSON keys or ids."
                ),
            },
            {
                "role": "user",
                "content": translation_prompt(batch),
            },
        ],
        "max_tokens": max(1024, min(8192, total_chars * 2)),
        "temperature": 0.1,
        "top_p": 0.7,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                by_id = parse_translation_response(message["content"])
                batch_results = [
                    (unit_index, by_id[unit_index])
                    for unit_index, _ in batch
                    if unit_index in by_id
                ]
                return batch_index, batch_results
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"translation batch {batch_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"translation batch {batch_index} failed")


def translate_unit_text(unit_index: int, text: str, api_key: str) -> str:
    payload = {
        "model": SILICONFLOW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are translating academic PDF text from English into Simplified Chinese. "
                    "Use precise, fluent academic Chinese. Keep the original meaning complete and do not summarize. "
                    "Preserve terminology, proper nouns, citations, numbers, units, formulas, and inline variables. "
                    "Return only the translated Chinese text. Do not add explanations, notes, markdown, or quotes."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "max_tokens": max(512, min(8192, len(text) * 2)),
        "temperature": 0.1,
        "top_p": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                return clean_translation(message["content"].strip())
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"translation unit {unit_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"translation unit {unit_index} failed")


def translate_blocks(pages: list[PageData], api_key: str, logger: Logger, progress_callback=None, line_height: float = DEFAULT_TEXT_LINE_HEIGHT) -> list[str]:
    blocks = page_blocks(pages)
    texts = [block.text for _, block in blocks]
    results = texts[:]
    references_start = find_references_range(pages)
    units = translation_units(blocks, references_start)
    active = [(index, unit.text) for index, unit in enumerate(units)]
    batches = translation_batches(active)

    logger.write(
        f"  text blocks: {len(texts)}, translation units: {len(active)}, "
        f"blocks selected for translation: {sum(len(unit.indexes) for unit in units)}, batches: {len(batches)}"
    )
    if progress_callback and hasattr(progress_callback, "on_blocks_analyzed"):
        progress_callback.on_blocks_analyzed(len(texts), len(active), len(batches))

    if not batches:
        return results

    workers = min(MAX_CONCURRENT_BATCHES, len(batches))
    total_batches = len(batches)
    completed_batches = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    future_batches = {
        executor.submit(translate_batch, batch_index, batch, api_key): batch
        for batch_index, batch in enumerate(batches, start=1)
    }
    future_labels = {
        future: batch_index
        for batch_index, future in enumerate(future_batches, start=1)
    }
    try:
        while future_batches:
            future = next(as_completed(future_batches))
            source_batch = future_batches.pop(future)
            label = future_labels.pop(future)
            try:
                batch_index, batch_results = future.result()
            except Exception:
                for pending in future_batches:
                    pending.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise

            translated_ids = {unit_index for unit_index, _ in batch_results}
            missing = [
                (unit_index, text)
                for unit_index, text in source_batch
                if unit_index not in translated_ids
            ]
            if missing:
                missing_ids = ", ".join(str(unit_index) for unit_index, _ in missing)
                raise RuntimeError(f"translation batch {label} missing ids: {missing_ids}")

            for unit_index, translated in batch_results:
                unit = units[unit_index]
                unit_blocks = [blocks[text_index][1] for text_index in unit.indexes]
                parts = split_translation(translated, unit_blocks, line_height)
                for text_index, part in zip(unit.indexes, parts, strict=True):
                    results[text_index] = part

            completed_batches += 1
            logger.write(f"  translated batch {batch_index}/{total_batches}")
            if progress_callback and hasattr(progress_callback, "on_batch_complete"):
                progress_callback.on_batch_complete(completed_batches, total_batches)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return results


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


def clean_translation(text: str) -> str:
    text = re.sub(r"</?(?:sub|sup)>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s*\n\s*\n\s*", "\n\n", text)
    text = re.sub(r"(?<!\n)\s*\n\s*(?!\n)", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


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


def estimate_font_size(rect: fitz.Rect, text: str, block: TextBlock, regular_body_size: float | None, line_height: float) -> float:
    limit = 18.0 if (block.align == "center" or block.bold or block.line_count <= 2) else 10.0
    source_size = (
        regular_body_size
        if regular_body_size
        and block.align == "left"
        and not block.bold
        and not block.italic
        and not block.leading_bold
        and block.line_count >= 2
        and rect.width >= 170
        and 6 <= block.font_size <= 12
        else block.font_size
    )
    size = min(source_size, limit)
    chars_per_line = max(1, int(rect.width / (size * 0.9)))
    line_count = (len(text) + chars_per_line - 1) // chars_per_line
    if line_count * size * line_height > rect.height * 1.35:
        while size > MIN_FONT_SIZE:
            chars_per_line = max(1, int(rect.width / (size * 0.9)))
            line_count = (len(text) + chars_per_line - 1) // chars_per_line
            if line_count * size * line_height <= rect.height * 1.2:
                return size
            size -= 0.5
    return size


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
        block.bold
        or block.italic
        or block.leading_bold
        or "**" in translated
        or any(char in translated for char in "，。！？；：、）】》”’")
    )


def write_fast_translation(
    page: fitz.Page,
    block: TextBlock,
    translated: str,
    font_size: float,
    line_height: float,
) -> bool:
    rect = fitz.Rect(block.rect)
    align = text_align_value(block.align)
    size = font_size
    while size >= MIN_FONT_SIZE:
        shape = page.new_shape()
        spare_height = shape.insert_textbox(
            rect,
            translated,
            fontfile=CJK_REGULAR_FONT,
            fontname=FAST_CJK_FONT_NAME,
            fontsize=size,
            lineheight=line_height,
            align=align,
        )
        if spare_height >= 0:
            shape.commit(overlay=True)
            return True
        size -= 0.5
    return False


def write_translation(
    page: fitz.Page,
    block: TextBlock,
    translated: str,
    archive: fitz.Archive,
    regular_body_size: float | None,
    line_height: float,
) -> str:
    rect = fitz.Rect(block.rect)
    font_size = estimate_font_size(rect, translated, block, regular_body_size, line_height)
    if can_use_fast_textbox(block, translated) and write_fast_translation(page, block, translated, font_size, line_height):
        return "fast"

    html_text = styled_html(translated, block)
    css = textbox_css(font_size, block.align, line_height)
    page.insert_htmlbox(
        rect,
        html_text,
        css=css,
        archive=archive,
        scale_low=0.4,
        overlay=True,
    )
    return "html"


def union_rect(blocks: list[TextBlock]) -> fitz.Rect:
    rect = fitz.Rect(blocks[0].rect)
    for block in blocks[1:]:
        rect.include_rect(fitz.Rect(block.rect))
    return rect


def group_block(group: LayoutGroup) -> TextBlock:
    first = group.blocks[0]
    rect = union_rect(group.blocks)
    return TextBlock(
        rect=tuple(rect),
        text=first.text,
        font_size=first.font_size,
        line_count=sum(block.line_count for block in group.blocks),
        bold=first.bold,
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
) -> str:
    return write_translation(page, group_block(group), group.text, archive, regular_body_size, line_height)


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
        for block_index, block in enumerate(page_data.blocks):
            translated = translations[translation_index]
            translation_index += 1
            if translated == block.text:
                continue
            page_translations.append((block_index, block, translated))

        covered_blocks += cover_original_text(target_page, [block for _, block, _ in page_translations])
        for group in layout_groups(page_data, page_translations, references_start):
            layout_mode = write_layout_group(target_page, group, archive, regular_body_size, line_height)
            if layout_mode == "fast":
                fast_layout_calls += 1
            else:
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


def output_path_for(pdf_path: Path) -> Path:
    return OUTPUT_DIR / f"{pdf_path.stem}_{TARGET_LANGUAGE}.pdf"


def collect_pdfs(paths: list[Path]) -> list[Path]:
    pdfs = []
    for path in paths or [SOURCE_DIR]:
        if path.is_dir():
            pdfs.extend(sorted(path.rglob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            pdfs.append(path)
    return sorted(dict.fromkeys(path.resolve() for path in pdfs))


def parse_line_height(value: str) -> float:
    try:
        line_height = float(value)
    except ValueError as exc:
        raise ValueError("line height must be a number") from exc
    if not 0.7 <= line_height <= 2.0:
        raise ValueError("line height must be between 0.7 and 2.0")
    return line_height


def translate_pdf(
    pdf_path: Path,
    api_key: str,
    logger: Logger,
    progress_callback=None,
    line_height: float = DEFAULT_TEXT_LINE_HEIGHT,
) -> Path:
    output_path = output_path_for(pdf_path)
    logger.write()
    logger.write(f"PDF: {pdf_path}")
    logger.write(f"OUT: {output_path}")
    if progress_callback and hasattr(progress_callback, "on_start"):
        progress_callback.on_start(pdf_path, output_path)

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages = extract_pages(doc)
        logger.write(f"  pages: {len(pages)}")
        if progress_callback and hasattr(progress_callback, "on_pages_extracted"):
            progress_callback.on_pages_extracted(len(pages))

        translations = translate_blocks(pages, api_key, logger, progress_callback, line_height)
        build_pdf(doc, pages, translations, output_path, logger, line_height)
        logger.write(f"  saved: {output_path}")
        if progress_callback and hasattr(progress_callback, "on_done"):
            progress_callback.on_done(output_path)
        return output_path
    except Exception as exc:
        if progress_callback and hasattr(progress_callback, "on_failed"):
            progress_callback.on_failed(str(exc))
        raise exc
    finally:
        doc.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate PDFs from original_PDF to processed_PDF.")
    parser.add_argument("paths", nargs="*", type=Path, help="PDF files or directories. Defaults to original_PDF.")
    parser.add_argument(
        "--line-height",
        type=parse_line_height,
        default=None,
        help=f"Typesetting line height multiplier. Defaults to {DEFAULT_TEXT_LINE_HEIGHT:g}; can also be set with {TYPESETTING_LINE_HEIGHT_ENV}.",
    )
    args = parser.parse_args(argv)
    if args.line_height is None:
        try:
            args.line_height = parse_line_height(os.getenv(TYPESETTING_LINE_HEIGHT_ENV, str(DEFAULT_TEXT_LINE_HEIGHT)))
        except ValueError as exc:
            parser.error(f"{TYPESETTING_LINE_HEIGHT_ENV}: {exc}")
    return args


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    logger = Logger(LOG_PATH)
    try:
        api_key = os.getenv(TRANSLATE_API_KEY_ENV)
        if not api_key:
            logger.write(f"ERROR: {TRANSLATE_API_KEY_ENV} not found in .env")
            return 1

        args = parse_args(argv or [])
        pdfs = collect_pdfs(args.paths)
        if not pdfs:
            logger.write(f"ERROR: no PDF files found in {SOURCE_DIR}")
            return 1

        logger.write(f"Found {len(pdfs)} PDF file(s).")
        logger.write(f"Typesetting line height: {args.line_height:g}")
        failed = []
        workers = min(MAX_CONCURRENT_PDFS, len(pdfs))
        logger.write(f"PDF workers: {workers}, global LLM request limit: {MAX_CONCURRENT_BATCHES}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(translate_pdf, pdf_path, api_key, logger, None, args.line_height): pdf_path
                for pdf_path in pdfs
            }
            for future in as_completed(futures):
                pdf_path = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failed.append((pdf_path, exc))
                    logger.write(f"FAILED: {pdf_path}: {exc}")

        logger.write()
        logger.write(f"Done. succeeded: {len(pdfs) - len(failed)}, failed: {len(failed)}")
        return 1 if failed else 0
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
