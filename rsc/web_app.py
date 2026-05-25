import asyncio
import json
import os
import queue
import shutil
import threading
import time
import uuid
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
    ROOT,
    SOURCE_DIR,
    translate_pdf,
)

app = FastAPI(title="PDF Translation Workbench API")

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


def resolve_pdf_file(directory: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name:
        raise HTTPException(status_code=404, detail="File not found")

    root = directory.resolve()
    file_path = (directory / name).resolve()
    if file_path.parent != root or not file_path.is_file():
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
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")

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
                        raise ValueError("GOOGLE_TRANSLATE_API_KEY not found in .env")

                    pdf_path = Path(job["original_path"])
                    translate_pdf(pdf_path, api_key, logger, progress_callback=cb)
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


# Start background worker thread
worker_thread = threading.Thread(target=worker_thread_func, daemon=True)
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
                        "filename": job["filename"]
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

    created_jobs = []
    for file in files:
        original_name = clean_upload_filename(file.filename)
        safe_name = get_safe_filename(SOURCE_DIR, original_name)
        dest_path = SOURCE_DIR / safe_name

        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        job_id = str(uuid.uuid4())
        job_data = {
            "id": job_id,
            "filename": safe_name,
            "original_path": str(dest_path),
            "translated_path": None,
            "status": "queued",
            "progress": {
                "pages": 0,
                "blocks": 0,
                "units": 0,
                "batches_total": 0,
                "batches_completed": 0
            },
            "logs": [f"Job initialized. Original file saved as: {safe_name}"],
            "error": None,
            "created_at": time.time()
        }

        with jobs_lock:
            jobs[job_id] = job_data

        job_queue.put(job_id)
        created_jobs.append(job_data)

    return created_jobs


@app.get("/api/jobs")
async def get_jobs():
    with jobs_lock:
        return sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)


@app.get("/api/jobs/{id}")
async def get_job(id: str):
    with jobs_lock:
        job = jobs.get(id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job


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
