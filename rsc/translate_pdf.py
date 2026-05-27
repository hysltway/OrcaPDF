from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
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
TARGET_LANGUAGE = "zh-CN"
CJK_REGULAR_FONT = "C:/Windows/Fonts/STSONG.TTF"
CJK_BOLD_FONT = "C:/Windows/Fonts/simsunb.ttf"
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
MIN_FONT_SIZE = 4.5
MAX_CONCURRENT_BATCHES = 12
MAX_BATCH_ITEMS = 6
MAX_BATCH_CHARS = 4000


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

    def write(self, message: str = "") -> None:
        self.file.write(message + "\n")
        self.file.flush()
        try:
            print(message)
        except UnicodeEncodeError:
            print(message.encode("ascii", errors="replace").decode("ascii"))
        if self.progress_callback and hasattr(self.progress_callback, "on_log"):
            self.progress_callback.on_log(message)

    def close(self) -> None:
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


def translation_batches(items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    batches = []
    batch = []
    char_count = 0

    for item in items:
        text_len = len(item[1])
        if batch and (len(batch) >= MAX_BATCH_ITEMS or char_count + text_len > MAX_BATCH_CHARS):
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

        if previous is None or not can_merge(previous[1], previous[2], page, block):
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


def split_translation(text: str, blocks: list[TextBlock]) -> list[str]:
    if len(blocks) == 1:
        return [text]

    total = sum(max(1, len(block.text)) for block in blocks)
    parts = []
    start = 0
    for block in blocks[:-1]:
        target = start + round(len(text) * max(1, len(block.text)) / total)
        split_at = text.rfind("。", start, target + 20)
        if split_at == -1 or split_at <= start:
            split_at = text.rfind("，", start, target + 15)
        if split_at == -1 or split_at <= start:
            split_at = target
        parts.append(text[start:split_at + 1].strip())
        start = split_at + 1
    parts.append(text[start:].strip())
    return parts


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


def translate_batch(batch_index: int, batch: list[tuple[int, str]], api_key: str) -> tuple[int, list[tuple[int, str]]]:
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

    with requests.Session() as session:
        for attempt in range(1, 4):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=45,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                by_id = parse_translation_response(message["content"])
                missing = [(unit_index, text) for unit_index, text in batch if unit_index not in by_id]
                if missing:
                    for unit_index, translated in translate_plain_batch(missing, api_key):
                        by_id[unit_index] = translated
                return batch_index, [(unit_index, by_id[unit_index]) for unit_index, _ in batch]
            except Exception as exc:
                if attempt == 3:
                    raise RuntimeError(f"translation batch {batch_index} failed: {exc}") from exc
                time.sleep(2 * attempt)

    raise RuntimeError(f"translation batch {batch_index} failed")


def translate_single(unit_index: int, text: str, api_key: str) -> tuple[int, str]:
    return translate_plain_batch([(unit_index, text)], api_key)[0]


def translate_plain_batch(batch: list[tuple[int, str]], api_key: str) -> list[tuple[int, str]]:
    results = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with requests.Session() as session:
        for unit_index, text in batch:
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
                "max_tokens": max(512, min(4096, len(text) * 2)),
                "temperature": 0.1,
                "top_p": 0.7,
            }

            for attempt in range(1, 4):
                try:
                    response = session.post(
                        SILICONFLOW_CHAT_URL,
                        headers=headers,
                        json=payload,
                        timeout=45,
                    )
                    if response.status_code >= 400:
                        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                    message = response.json()["choices"][0]["message"]
                    results.append((unit_index, clean_translation(message["content"].strip())))
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise RuntimeError(f"translation unit {unit_index} failed: {exc}") from exc
                    time.sleep(2 * attempt)
    return results


def translate_blocks(pages: list[PageData], api_key: str, logger: Logger, progress_callback=None) -> list[str]:
    blocks = page_blocks(pages)
    texts = [block.text for _, block in blocks]
    results = texts[:]
    references_start = find_references_range(pages)
    units = translation_units(blocks, references_start)
    active = [(index, unit.text) for index, unit in enumerate(units)]
    batches = translation_batches(active)

    logger.write(
        f"  text blocks: {len(texts)}, translation units: {len(active)}, "
        f"covered blocks: {sum(len(unit.indexes) for unit in units)}, batches: {len(batches)}"
    )
    if progress_callback and hasattr(progress_callback, "on_blocks_analyzed"):
        progress_callback.on_blocks_analyzed(len(texts), len(active), len(batches))

    if not batches:
        return results

    workers = min(MAX_CONCURRENT_BATCHES, len(batches))
    completed_batches = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(translate_batch, batch_index, batch, api_key): batch_index
            for batch_index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(futures):
            try:
                batch_index, batch_results = future.result()
            except Exception as exc:
                batch_index = futures[future]
                logger.write(f"  batch {batch_index}/{len(batches)} failed, retrying once: {exc}")
                batch_results = [translate_single(unit_index, text, api_key) for unit_index, text in batches[batch_index - 1]]

            for unit_index, translated in batch_results:
                unit = units[unit_index]
                unit_blocks = [blocks[text_index][1] for text_index in unit.indexes]
                parts = split_translation(translated, unit_blocks)
                for text_index, part in zip(unit.indexes, parts, strict=True):
                    results[text_index] = part

            completed_batches += 1
            logger.write(f"  translated batch {batch_index}/{len(batches)}")
            if progress_callback and hasattr(progress_callback, "on_batch_complete"):
                progress_callback.on_batch_complete(completed_batches, len(batches))

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

    for char in text:
        char_class = "cjk" if is_cjk(char) else "latin"
        if current and char_class != current_class:
            parts.append(f'<span class="{current_class}">{html.escape("".join(current))}</span>')
            current = []
        current.append(char)
        current_class = char_class

    if current:
        parts.append(f'<span class="{current_class}">{html.escape("".join(current))}</span>')
    return "".join(parts)


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


def styled_html(text: str, block: TextBlock) -> str:
    if block.leading_bold:
        end = leading_style_end(text)
        if end:
            heading = text[:end]
            rest = text[end:]
            return (
                f'<span class="bold">{html_fragments(heading)}</span>'
                f'<span class="regular">{html_fragments(rest)}</span>'
            )
    if block.leading_bold and "。" in text:
        heading, rest = text.split("。", 1)
        return (
            f'<span class="bold">{html_fragments(heading + "。")}</span>'
            f'<span class="regular">{html_fragments(rest)}</span>'
        )
    return f'<span class="{style_class(block)}">{html_fragments(text)}</span>'


def clean_translation(text: str) -> str:
    text = re.sub(r"</?(?:sub|sup)>", "", text)
    return html.unescape(text)


@cache
def textbox_css(font_size: float, align: str) -> str:
    return f"""
{FONT_FACE_CSS}
body {{ margin: 0; font-size: {font_size}pt; line-height: 1.15; text-align: {align}; }}
.cjk {{ font-family: CjkRegularLocal; }}
.latin {{ font-family: TimesLocal; }}
.bold .cjk {{ font-family: CjkBoldLocal; }}
.bold .latin {{ font-family: TimesBoldLocal; }}
.italic .cjk {{ font-family: CjkRegularLocal; font-style: italic; }}
.italic .latin {{ font-family: TimesItalicLocal; }}
.bolditalic .cjk {{ font-family: CjkBoldLocal; font-style: italic; }}
.bolditalic .latin {{ font-family: TimesBoldItalicLocal; }}
"""


def estimate_font_size(rect: fitz.Rect, text: str, block: TextBlock) -> float:
    limit = 18.0 if (block.align == "center" or block.bold or block.line_count <= 2) else 10.0
    size = min(block.font_size, limit)
    while size > MIN_FONT_SIZE:
        chars_per_line = max(1, int(rect.width / (size * 0.95)))
        line_count = (len(text) + chars_per_line - 1) // chars_per_line
        if line_count * size * 1.2 <= rect.height:
            return size
        size -= 0.5
    return MIN_FONT_SIZE


def write_translation(page: fitz.Page, block: TextBlock, translated: str, archive: fitz.Archive) -> int:
    rect = fitz.Rect(block.rect)
    cover = rect + (-0.8, -0.8, 0.8, 0.8)
    page.draw_rect(cover, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)

    html_text = styled_html(translated, block)
    font_size = estimate_font_size(rect, translated, block)
    layout_calls = 0
    while font_size >= MIN_FONT_SIZE:
        layout_calls += 1
        css = textbox_css(font_size, block.align)
        spare_height, _ = page.insert_htmlbox(
            rect,
            html_text,
            css=css,
            archive=archive,
            scale_low=0.55,
            overlay=True,
        )
        if spare_height >= 0:
            return layout_calls
        font_size -= 0.5

    layout_calls += 1
    css = textbox_css(MIN_FONT_SIZE, block.align)
    page.insert_htmlbox(
        rect,
        html_text,
        css=css,
        archive=archive,
        scale_low=0.4,
        overlay=True,
    )
    return layout_calls


def build_pdf(source_doc: fitz.Document, pages: list[PageData], translations: list[str], output_path: Path, logger: Logger = None) -> None:
    output = fitz.open()
    output.insert_pdf(source_doc)
    translation_index = 0
    total_blocks = sum(len(page_data.blocks) for page_data in pages)
    processed_blocks = 0
    layout_calls = 0
    render_start = time.perf_counter()
    archive = fitz.Archive(FONT_DIR)

    if logger:
        logger.write(f"  typesetting PDF: writing {total_blocks} translated blocks...")

    for page_data in pages:
        target_page = output[page_data.index]
        for block in page_data.blocks:
            translated = translations[translation_index]
            translation_index += 1
            if translated == block.text:
                continue
            layout_calls += write_translation(target_page, block, translated, archive)
            processed_blocks += 1
            if logger and processed_blocks % 10 == 0:
                logger.write(f"    typesetting: rendered {processed_blocks}/{total_blocks} blocks...")

    if logger:
        render_seconds = time.perf_counter() - render_start
        logger.write(
            f"  typesetting: rendered {processed_blocks} changed blocks in "
            f"{render_seconds:.1f}s ({layout_calls} html layout calls)"
        )
        logger.write("  typesetting: saving finalized PDF output...")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    save_start = time.perf_counter()
    output.save(tmp_path, garbage=4, deflate=True)
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


def translate_pdf(pdf_path: Path, api_key: str, logger: Logger, progress_callback=None) -> Path:
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

        translations = translate_blocks(pages, api_key, logger, progress_callback)
        build_pdf(doc, pages, translations, output_path, logger)
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
    return parser.parse_args(argv)


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
        failed = []
        for pdf_path in pdfs:
            try:
                translate_pdf(pdf_path, api_key, logger)
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
