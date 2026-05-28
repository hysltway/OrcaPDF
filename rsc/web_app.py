import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import queue
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rsc.translate_pdf import (
    Logger,
    OUTPUT_DIR,
    DEFAULT_TEXT_LINE_HEIGHT,
    ROOT,
    SOURCE_DIR,
    TARGET_LANGUAGE,
    TYPESETTING_LINE_HEIGHT_ENV,
    TRANSLATE_API_KEY_ENV,
    MAX_CONCURRENT_PDFS,
    parse_line_height,
    translate_pdf,
)


class PdfRangeAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True

        _, method, path, _, status_code = args[:5]
        try:
            status = int(status_code)
        except (TypeError, ValueError):
            return True

        if method != "GET" or status != 206:
            return True

        return not str(path).startswith(("/api/files/original/", "/api/files/translated/"))


def install_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, PdfRangeAccessLogFilter) for existing in logger.filters):
        logger.addFilter(PdfRangeAccessLogFilter())


def install_connection_reset_filter() -> None:
    loop = asyncio.get_running_loop()
    if getattr(loop, "_pdf_translate_connection_reset_filter", False):
        return

    previous_handler = loop.get_exception_handler()

    def handle_exception(event_loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return

        if previous_handler:
            previous_handler(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)
    setattr(loop, "_pdf_translate_connection_reset_filter", True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_connection_reset_filter()
    yield


install_access_log_filter()

app = FastAPI(title="PDF Translation Workbench API", lifespan=lifespan)
mimetypes.add_type("application/javascript", ".mjs")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Thread-safe in-memory store for jobs
jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()
TRANSLATED_SUFFIX = f"_{TARGET_LANGUAGE}.pdf"


def clean_upload_filename(filename: str | None) -> str:
    name = Path((filename or "").replace("\\", "/")).name
    if not name:
        raise HTTPException(status_code=400, detail="PDF filename is required")
    if Path(name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    return name


def get_safe_filename(directory: Path, filename: str) -> str:
    path = Path(filename)
    stem = path.stem
    suffix = path.suffix
    
    # Standardize suffix to lowercase check
    if not (directory / filename).exists():
        return filename
        
    counter = 1
    while True:
        new_filename = f"{stem}_{counter}{suffix}"
        if not (directory / new_filename).exists():
            return new_filename
        counter += 1


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_digest(file: UploadFile) -> str:
    digest = hashlib.sha256()
    file.file.seek(0)
    for chunk in iter(lambda: file.file.read(1024 * 1024), b""):
        digest.update(chunk)
    file.file.seek(0)
    return digest.hexdigest()


def new_progress() -> dict:
    return {
        "pages": 0,
        "blocks": 0,
        "units": 0,
        "batches_total": 0,
        "batches_completed": 0
    }


def find_job_by_filename(filename: str) -> dict | None:
    for job in jobs.values():
        if job["filename"] == filename:
            return job
    return None


def output_path_for_source_name(filename: str) -> Path:
    return OUTPUT_DIR / f"{Path(filename).stem}_{TARGET_LANGUAGE}.pdf"


def restore_processed_jobs():
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with jobs_lock:
        for translated_path in OUTPUT_DIR.glob(f"*{TRANSLATED_SUFFIX}"):
            original_stem = translated_path.name[:-len(TRANSLATED_SUFFIX)]
            original_name = f"{original_stem}.pdf"
            original_path = SOURCE_DIR / original_name
            if not original_path.is_file() or find_job_by_filename(original_name):
                continue

            progress = new_progress()
            progress["batches_completed"] = 1
            progress["batches_total"] = 1
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{original_name}:{translated_path.name}"))
            jobs[job_id] = {
                "id": job_id,
                "filename": original_name,
                "original_path": str(original_path),
                "translated_path": str(translated_path),
                "status": "done",
                "progress": progress,
                "logs": [f"Restored processed file: {translated_path.name}"],
                "error": None,
                "created_at": translated_path.stat().st_mtime
            }


def find_duplicate_source(digest: str) -> Path | None:
    for pdf_path in SOURCE_DIR.glob("*.pdf"):
        if file_digest(pdf_path) == digest:
            return pdf_path
    return None


def create_job_data(source_path: Path, status: str = "queued", translated_path: Path | None = None) -> dict:
    progress = new_progress()
    if status == "done":
        progress["batches_completed"] = 1
        progress["batches_total"] = 1

    return {
        "id": str(uuid.uuid4()),
        "filename": source_path.name,
        "original_path": str(source_path),
        "translated_path": str(translated_path) if translated_path else None,
        "status": status,
        "progress": progress,
        "logs": [f"Job initialized. Original file saved as: {source_path.name}"],
        "error": None,
        "created_at": time.time()
    }


import urllib.parse

def resolve_pdf_file(directory: Path, name: str) -> Path:
    decoded_name = urllib.parse.unquote(name)
    if not decoded_name or "/" in decoded_name or "\\" in decoded_name:
        raise HTTPException(status_code=404, detail="File not found")

    root = directory.resolve()
    file_path = (directory / decoded_name).resolve()
    if not file_path.is_file() or file_path.parent != root:
        file_path = (directory / name).resolve()
        if not file_path.is_file() or file_path.parent != root:
            raise HTTPException(status_code=404, detail="File not found")
    return file_path


class JobProgressCallback:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def on_start(self, pdf_path: Path, output_path: Path):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["status"] = "running"

    def on_pages_extracted(self, num_pages: int):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["progress"]["pages"] = num_pages

    def on_blocks_analyzed(self, num_blocks: int, num_units: int, num_batches: int):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["progress"]["blocks"] = num_blocks
                jobs[self.job_id]["progress"]["units"] = num_units
                jobs[self.job_id]["progress"]["batches_total"] = num_batches

    def on_batch_complete(self, completed_batches: int, total_batches: int):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["progress"]["batches_completed"] = completed_batches
                jobs[self.job_id]["progress"]["batches_total"] = total_batches

    def on_log(self, message: str):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["logs"].append(message)

    def on_done(self, output_path: Path):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["status"] = "done"
                jobs[self.job_id]["translated_path"] = str(output_path)

    def on_failed(self, error_message: str):
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["status"] = "failed"
                jobs[self.job_id]["error"] = error_message


def worker_thread_func():
    load_dotenv(ROOT / ".env")
    provider = os.getenv("TRANSLATE_PROVIDER", "siliconflow").lower()
    if provider == "google":
        api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
        key_name = "GOOGLE_TRANSLATE_API_KEY"
    else:
        api_key = os.getenv("siliconflow_TRANSLATE_API_KEY")
        key_name = "siliconflow_TRANSLATE_API_KEY"

    try:
        line_height = parse_line_height(os.getenv(TYPESETTING_LINE_HEIGHT_ENV, str(DEFAULT_TEXT_LINE_HEIGHT)))
    except ValueError as exc:
        line_height = DEFAULT_TEXT_LINE_HEIGHT
        line_height_error = f"{TYPESETTING_LINE_HEIGHT_ENV}: {exc}"
    else:
        line_height_error = None

    while True:
        try:
            job_id = job_queue.get()
            if job_id is None:
                break

            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    job_queue.task_done()
                    continue
                job["status"] = "running"

            try:
                job_log_path = OUTPUT_DIR / f"{job_id}.log"
                cb = JobProgressCallback(job_id)
                logger = Logger(job_log_path, progress_callback=cb)
                try:
                    if not api_key:
                        raise ValueError(f"{key_name} not found in .env for provider '{provider}'")
                    if line_height_error:
                        raise ValueError(line_height_error)

                    pdf_path = Path(job["original_path"])
                    translate_pdf(pdf_path, api_key, logger, progress_callback=cb, line_height=line_height)
                finally:
                    logger.close()
                    if job_log_path.exists():
                        try:
                            job_log_path.unlink()
                        except OSError:
                            pass

            except Exception as e:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["status"] = "failed"
                        jobs[job_id]["error"] = str(e)
                        jobs[job_id]["logs"].append(f"CRITICAL ERROR: {str(e)}")

            job_queue.task_done()
        except Exception as e:
            print(f"Worker thread error: {e}")
            time.sleep(1)


# Start background worker threads
worker_threads = [
    threading.Thread(target=worker_thread_func, daemon=True)
    for _ in range(MAX_CONCURRENT_PDFS)
]
for worker_thread in worker_threads:
    worker_thread.start()


async def event_generator(job_id: str):
    last_log_count = -1
    last_status = None
    last_batches_completed = -1

    while True:
        data = None
        should_break = False

        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                data = json.dumps({"error": "Job not found"})
                should_break = True
            else:
                progress = dict(job["progress"])
                logs = list(job["logs"])
                current_log_count = len(logs)
                current_status = job["status"]
                current_batches_completed = progress["batches_completed"]

                has_update = (
                    last_log_count == -1
                    or current_log_count > last_log_count
                    or current_status != last_status
                    or current_batches_completed != last_batches_completed
                )

                if has_update:
                    last_log_count = current_log_count
                    last_status = current_status
                    last_batches_completed = current_batches_completed

                    data = json.dumps({
                        "id": job_id,
                        "status": current_status,
                        "progress": progress,
                        "logs": logs,
                        "error": job["error"],
                        "filename": job["filename"],
                        "translated_path": job["translated_path"]
                    })

                should_break = current_status in ("done", "failed")

        if data is not None:
            yield f"data: {data}\n\n"
        if should_break:
            break

        await asyncio.sleep(0.2)


@app.post("/api/jobs")
async def create_jobs(files: List[UploadFile] = File(...)):
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    restore_processed_jobs()

    created_jobs = []
    for file in files:
        original_name = clean_upload_filename(file.filename)
        digest = upload_digest(file)
        duplicate_path = find_duplicate_source(digest)
        if duplicate_path:
            translated_path = output_path_for_source_name(duplicate_path.name)
            with jobs_lock:
                existing_job = find_job_by_filename(duplicate_path.name)
                if existing_job:
                    created_jobs.append(existing_job)
                    continue

                job_data = create_job_data(
                    duplicate_path,
                    "done" if translated_path.is_file() else "queued",
                    translated_path if translated_path.is_file() else None
                )
                jobs[job_data["id"]] = job_data

            if job_data["status"] == "queued":
                job_queue.put(job_data["id"])
            created_jobs.append(job_data)
            continue

        safe_name = get_safe_filename(SOURCE_DIR, original_name)
        dest_path = SOURCE_DIR / safe_name

        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        job_data = create_job_data(dest_path)

        with jobs_lock:
            jobs[job_data["id"]] = job_data

        job_queue.put(job_data["id"])
        created_jobs.append(job_data)

    return created_jobs


@app.get("/api/jobs")
async def get_jobs():
    restore_processed_jobs()
    with jobs_lock:
        return sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)


@app.get("/api/jobs/{id}")
async def get_job(id: str):
    with jobs_lock:
        job = jobs.get(id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job


@app.post("/api/jobs/{id}/retranslate")
async def retranslate_job(id: str):
    with jobs_lock:
        job = jobs.get(id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        source_path = Path(job["original_path"])
        if not source_path.is_file():
            raise HTTPException(status_code=404, detail="Original file not found")

        job["status"] = "queued"
        job["translated_path"] = None
        job["progress"] = new_progress()
        job["logs"] = [f"Retranslation queued for: {job['filename']}"]
        job["error"] = None
        job["created_at"] = time.time()

    job_queue.put(id)

    with jobs_lock:
        return jobs[id]


@app.get("/api/jobs/{id}/events")
async def get_job_events(id: str):
    with jobs_lock:
        if id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")

    return StreamingResponse(event_generator(id), media_type="text/event-stream")


@app.get("/api/files/original/{name}")
async def get_original_file(name: str):
    file_path = resolve_pdf_file(SOURCE_DIR, name)
    return FileResponse(file_path)


@app.get("/api/files/translated/{name}")
async def get_translated_file(name: str):
    file_path = resolve_pdf_file(OUTPUT_DIR, name)
    return FileResponse(file_path)


# Mount built frontend files if they exist for single-binary usage
frontend_dist = ROOT / "my-react-app" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
