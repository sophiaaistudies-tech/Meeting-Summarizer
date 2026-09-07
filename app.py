"""
Web interface for the Georgian meeting summarizer.

Upload a recording, enter the recipients, and the summary is emailed to them.
Access is protected by a passcode, because the app sends mail from your own
Gmail account and an open endpoint would be a spam relay.

Run locally:
    uvicorn app:app --reload --port 8000

Environment variables, in addition to the pipeline's own keys:
    APP_PASSCODE                 required, the passcode users must enter
    MAX_UPLOAD_MB                optional, default 25
    ALLOWED_RECIPIENT_DOMAINS    optional, comma separated, e.g. gah.ge,gmail.com
    MAX_JOBS_PER_HOUR            optional, default 5, per IP address
    ADMIN_EMAIL                  optional, gets a notification for every run
    TRUST_PROXY                  optional, set to 1 when running behind a proxy
                                 such as Railway, so the real visitor IP is used
"""

import os
import re
import sys
import uuid
import time
import secrets
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent / "src"))
from transcribe import transcribe          # noqa: E402
from summarize import summarize            # noqa: E402
from mailer import send                    # noqa: E402
from notify import notify_admin            # noqa: E402

APP_PASSCODE = os.getenv("APP_PASSCODE", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_JOBS_PER_HOUR = int(os.getenv("MAX_JOBS_PER_HOUR", "5"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in os.getenv("ALLOWED_RECIPIENT_DOMAINS", "").split(",")
    if d.strip()
]

ALLOWED_EXTENSIONS = {
    ".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg", ".opus",
    ".flac", ".webm", ".mov", ".mpeg", ".mpga",
}
MAX_RECIPIENTS = 5
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")

app = FastAPI(title="Meeting Summarizer", docs_url=None, redoc_url=None)

# job_id -> {stage, error, title, recipients}
_jobs = {}
_jobs_lock = threading.Lock()
_rate = {}  # ip -> [timestamps]
_rate_lock = threading.Lock()


def _fail_closed():
    """Refuse to serve if the passcode is unset, rather than running open."""
    if not APP_PASSCODE:
        raise HTTPException(
            status_code=503,
            detail="APP_PASSCODE is not set on the server, so uploads are disabled.",
        )


def _set(job_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _client_ip(request: Request) -> str:
    """
    The visitor's address.

    Behind a proxy such as Railway, request.client.host is the proxy, so every
    visitor would share one rate limit bucket. X-Forwarded-For holds the chain,
    with the original client first. Only trusted when TRUST_PROXY is set, because
    the header is trivially forged when the app is reachable directly.
    """
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _rate_limited(ip) -> bool:
    now = time.time()
    with _rate_lock:
        recent = [t for t in _rate.get(ip, []) if now - t < 3600]
        if len(recent) >= MAX_JOBS_PER_HOUR:
            _rate[ip] = recent
            return True
        recent.append(now)
        _rate[ip] = recent
        return False


def _parse_recipients(raw: str):
    parts = [p.strip() for p in re.split(r"[,\s;]+", raw or "") if p.strip()]
    if not parts:
        raise HTTPException(400, "Enter at least one email address.")
    if len(parts) > MAX_RECIPIENTS:
        raise HTTPException(400, f"Maximum {MAX_RECIPIENTS} recipients per summary.")
    for p in parts:
        if not EMAIL_RE.match(p):
            raise HTTPException(400, f"This does not look like an email address: {p}")
        if ALLOWED_DOMAINS and p.split("@")[1].lower() not in ALLOWED_DOMAINS:
            raise HTTPException(
                400, f"Summaries can only be sent to: {', '.join(ALLOWED_DOMAINS)}"
            )
    return parts


def _process(job_id: str, audio_path: str, recipients: list, meta: dict = None):
    """Runs in a background thread. Every stage failure is reported to the UI."""
    meta = meta or {}
    started = time.time()
    outcome, detail = "failed", ""
    try:
        _set(job_id, stage="transcribing")
        transcript = transcribe(audio_path)
        if not transcript.strip():
            raise ValueError("No speech was found in the recording.")

        _set(job_id, stage="summarizing")
        data = summarize(transcript)

        _set(job_id, stage="sending")
        send(data, transcript, recipients)

        _set(job_id, stage="done", title=data.get("title", ""))
        outcome, detail = "worked", data.get("title", "")
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        _set(job_id, stage="failed", error=detail)
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass

        # A notification must never be able to fail the job it is reporting on
        try:
            notify_admin(
                f"Meeting summarizer: {outcome}",
                {
                    "Outcome": outcome,
                    "Meeting": detail if outcome == "worked" else "",
                    "Error": "" if outcome == "worked" else detail,
                    "Sent to": ", ".join(recipients),
                    "Recording": meta.get("filename", ""),
                    "Size": meta.get("size", ""),
                    "From IP": meta.get("ip", ""),
                    "Took": f"{int(time.time() - started)}s",
                },
            )
        except Exception as e:
            print(f"[Notify] Suppressed: {type(e).__name__}: {e}")


@app.get("/", response_class=HTMLResponse)
def index():
    return TEMPLATE


@app.get("/healthz")
def healthz():
    return {"ok": True, "passcode_set": bool(APP_PASSCODE)}


@app.post("/api/jobs")
async def create_job(
    request: Request,
    background_tasks: BackgroundTasks,
    passcode: str = Form(""),
    recipients: str = Form(""),
    file: UploadFile = File(...),
):
    _fail_closed()

    if not secrets.compare_digest(passcode.strip(), APP_PASSCODE):
        raise HTTPException(401, "Wrong passcode.")

    ip = _client_ip(request)
    if _rate_limited(ip):
        raise HTTPException(
            429, f"Limit of {MAX_JOBS_PER_HOUR} summaries an hour reached. Try later."
        )

    to = _parse_recipients(recipients)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            "Unsupported file type. Use an audio or video recording, "
            "for example mp3, m4a, wav or mp4.",
        )

    # Stream to disk with a hard size ceiling, so a large upload cannot fill the box
    limit = MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Recording is over {MAX_UPLOAD_MB} MB.")
                out.write(chunk)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    if size == 0:
        os.remove(tmp_path)
        raise HTTPException(400, "The file is empty.")

    job_id = uuid.uuid4().hex
    _set(job_id, stage="queued", error=None, title="", recipients=to)
    meta = {
        "filename": file.filename or "",
        "size": f"{size / 1048576:.1f} MB",
        "ip": ip,
    }
    background_tasks.add_task(_process, job_id, tmp_path, to, meta)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return JSONResponse(
        {
            "stage": job.get("stage"),
            "error": job.get("error"),
            "title": job.get("title", ""),
            "recipients": job.get("recipients", []),
        }
    )
