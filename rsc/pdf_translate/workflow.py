from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
from dotenv import load_dotenv

from .config import (
    DEFAULT_TEXT_LINE_HEIGHT,
    LOG_PATH,
    MAX_CONCURRENT_BATCHES,
    MAX_CONCURRENT_PDFS,
    OUTPUT_DIR,
    ROOT,
    SOURCE_DIR,
    TARGET_LANGUAGE,
    TYPESETTING_LINE_HEIGHT_ENV,
)
from .extraction import extract_pages
from .models import Logger
from .render import build_pdf
from .translator import translate_blocks

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
        provider = os.getenv("TRANSLATE_PROVIDER", "siliconflow").lower()
        if provider == "google":
            api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
            key_name = "GOOGLE_TRANSLATE_API_KEY"
        else:
            api_key = os.getenv("siliconflow_TRANSLATE_API_KEY")
            key_name = "siliconflow_TRANSLATE_API_KEY"

        if not api_key:
            logger.write(f"ERROR: {key_name} not found in .env for provider '{provider}'")
            return 1

        logger.write(f"Translation provider: {provider}")

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

