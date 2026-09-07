# NOTIFY.md

Adds usage notifications: an email to you every time someone runs a summary,
whether it worked or failed.

## How to use it

1. Save this file into `C:\Users\Mylaptop\meeting-summarizer` as `NOTIFY.md`
2. Open PowerShell there and run `claude`
3. Send one message:

   > Read NOTIFY.md in this folder and carry out the instructions in it.

Do the BUILD.md work first. This assumes the web interface is already in place.

---

## Instructions for Claude Code

Work through these in order. Stop and tell the user if a step fails.

**1. Create or overwrite the files below**, exactly as given, UTF-8 without a byte
order mark. `src/notify.py` is new. The other four replace what is there.

**2. Add this line to `.env`**, then ask the user which address should receive the
notifications and write their answer in:

```
ADMIN_EMAIL=
```

Leaving it empty switches notifications off, which is a supported state, not a
broken one.

**3. Run `pytest -q`.** It must report 26 passed. Four of those are new and cover
notifications on success, on failure, that a broken notifier cannot fail the job,
and that an empty `ADMIN_EMAIL` disables sending.

**4. Test it for real.** Start `uvicorn app:app --port 8000`, ask the user to run
one summary through the page, and confirm two emails arrive: the summary itself,
and a separate notification. Wait for their answer.

**5. Commit and push once they confirm**, with the message:
`Notify the operator on every run, worked or failed`

Then show `git ls-files` and confirm `.env` is not in it.

---

## `app.py`

```python
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
"""

import os
import re
import sys
import uuid
import time
import secrets
import shutil
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

    ip = request.client.host if request.client else "unknown"
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
```

## `src/notify.py`

```python
"""
Usage notifications.

Sends a short plain-text email to ADMIN_EMAIL every time someone runs a summary,
so you know the page is being used, by whom, and whether it worked.

Notifications never interrupt the job: if sending one fails, it is logged and the
summary still goes out.

Set ADMIN_EMAIL in .env. Leave it empty to switch notifications off.
"""

import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()


def notify_admin(subject: str, fields: dict) -> bool:
    """
    Send one notification. Returns True if it went out, False otherwise.

    fields is rendered as aligned "label: value" lines, in the order given.
    """
    if not ADMIN_EMAIL:
        return False
    if not (EMAIL_SENDER and EMAIL_PASSWORD):
        print("[Notify] EMAIL_SENDER or EMAIL_PASSWORD missing, skipping notification.")
        return False

    width = max((len(k) for k in fields), default=0)
    body = "\n".join(f"{k.ljust(width)}  {v}" for k, v in fields.items())
    body += f"\n\nSent {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ADMIN_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[Notify] Sent to {ADMIN_EMAIL}")
        return True
    except Exception as e:
        # A failed notification must never fail the summary itself
        print(f"[Notify] Failed: {type(e).__name__}: {e}")
        return False
```

## `tests/test_app.py`

```python
"""
Tests for the web layer. The three pipeline stages are replaced with fakes,
so these run without API keys and without sending mail.

    pytest -q
"""

import io
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

os.environ["APP_PASSCODE"] = "test-code"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["MAX_JOBS_PER_HOUR"] = "100"

import app as webapp  # noqa: E402

SENT = []


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(webapp, "transcribe", lambda p: "speaker_0: გამარჯობა")
    monkeypatch.setattr(
        webapp, "summarize",
        lambda t: {"title": "ტესტი", "summary": "ok", "decisions": [],
                   "action_items": [], "unresolved": []},
    )
    monkeypatch.setattr(webapp, "send", lambda d, t, r: SENT.append(r))
    with webapp._jobs_lock:
        webapp._jobs.clear()
    with webapp._rate_lock:
        webapp._rate.clear()


@pytest.fixture
def client():
    return TestClient(webapp.app)


def upload(client, **kw):
    data = {"passcode": kw.get("passcode", "test-code"),
            "recipients": kw.get("recipients", "a@b.ge")}
    name = kw.get("filename", "meeting.m4a")
    content = kw.get("content", b"\x00" * 2048)
    return client.post("/api/jobs", data=data,
                       files={"file": (name, io.BytesIO(content), "audio/mp4")})


def wait_done(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["stage"] in ("done", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---------- page and health ----------

def test_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "შეხვედრის შეჯამება" in r.text
    assert 'id="drop"' in r.text and 'id="passcode"' in r.text


def test_healthz(client):
    d = client.get("/healthz").json()
    assert d["ok"] is True and d["passcode_set"] is True


# ---------- happy path ----------

def test_full_job_sends_mail(client):
    r = upload(client, recipients="one@b.ge, two@c.ge")
    assert r.status_code == 200
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "done", d
    assert d["title"] == "ტესტი"
    assert SENT == [["one@b.ge", "two@c.ge"]]


def test_temp_file_is_deleted(client, monkeypatch):
    seen = {}

    def spy(path):
        seen["path"] = path
        assert os.path.exists(path), "file should exist during processing"
        return "speaker_0: ok"

    monkeypatch.setattr(webapp, "transcribe", spy)
    r = upload(client)
    wait_done(client, r.json()["job_id"])
    assert not os.path.exists(seen["path"]), "temp file must be removed after the job"


# ---------- access control ----------

def test_wrong_passcode_rejected(client):
    r = upload(client, passcode="nope")
    assert r.status_code == 401


def test_empty_passcode_rejected(client):
    r = upload(client, passcode="")
    assert r.status_code == 401


def test_no_passcode_configured_disables_uploads(client, monkeypatch):
    monkeypatch.setattr(webapp, "APP_PASSCODE", "")
    r = upload(client)
    assert r.status_code == 503


# ---------- validation ----------

@pytest.mark.parametrize("bad", ["", "   ", "notanemail", "a@b", "a@b.ge, broken@"])
def test_bad_recipients_rejected(client, bad):
    assert upload(client, recipients=bad).status_code == 400


def test_too_many_recipients(client):
    many = ", ".join(f"p{i}@b.ge" for i in range(6))
    assert upload(client, recipients=many).status_code == 400


def test_recipient_domain_allowlist(client, monkeypatch):
    monkeypatch.setattr(webapp, "ALLOWED_DOMAINS", ["gah.ge"])
    assert upload(client, recipients="x@gmail.com").status_code == 400
    assert upload(client, recipients="x@gah.ge").status_code == 200


def test_bad_extension_rejected(client):
    assert upload(client, filename="notes.pdf").status_code == 400
    assert upload(client, filename="script.exe").status_code == 400


def test_empty_file_rejected(client):
    assert upload(client, content=b"").status_code == 400


def test_oversized_file_rejected(client):
    # MAX_UPLOAD_MB is 1 in this test run
    r = upload(client, content=b"\x00" * (2 * 1024 * 1024))
    assert r.status_code == 413


def test_recipients_accept_semicolons_and_newlines(client):
    r = upload(client, recipients="one@b.ge;\n two@c.ge")
    assert r.status_code == 200
    wait_done(client, r.json()["job_id"])
    assert SENT == [["one@b.ge", "two@c.ge"]]


# ---------- failure reporting ----------

def test_pipeline_error_surfaces_to_client(client, monkeypatch):
    def boom(p):
        raise RuntimeError("ElevenLabs quota exceeded")

    monkeypatch.setattr(webapp, "transcribe", boom)
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "failed"
    assert "quota exceeded" in d["error"]


def test_silent_recording_reported(client, monkeypatch):
    monkeypatch.setattr(webapp, "transcribe", lambda p: "   ")
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "failed"
    assert "No speech" in d["error"]


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404


# ---------- rate limiting ----------

def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(webapp, "MAX_JOBS_PER_HOUR", 2)
    assert upload(client).status_code == 200
    assert upload(client).status_code == 200
    assert upload(client).status_code == 429


# ---------- usage notifications ----------

def test_notification_sent_on_success(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: seen.update(subject=subject, fields=fields))
    r = upload(client, filename="board.m4a", recipients="a@b.ge")
    wait_done(client, r.json()["job_id"])
    assert "worked" in seen["subject"]
    f = seen["fields"]
    assert f["Outcome"] == "worked"
    assert f["Meeting"] == "ტესტი"
    assert f["Sent to"] == "a@b.ge"
    assert f["Recording"] == "board.m4a"
    assert f["Size"].endswith("MB")
    assert f["Took"].endswith("s")
    assert f["Error"] == ""


def test_notification_sent_on_failure(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: seen.update(subject=subject, fields=fields))
    monkeypatch.setattr(webapp, "transcribe",
                        lambda p: (_ for _ in ()).throw(RuntimeError("quota gone")))
    r = upload(client)
    wait_done(client, r.json()["job_id"])
    assert "failed" in seen["subject"]
    assert "quota gone" in seen["fields"]["Error"]
    assert seen["fields"]["Meeting"] == ""


def test_failed_notification_does_not_break_the_job(client, monkeypatch):
    def broken(subject, fields):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(webapp, "notify_admin", broken)
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    # the summary itself still succeeded and the mail still went out
    assert d["stage"] == "done", d
    assert SENT == [["a@b.ge"]]


def test_notifications_off_when_admin_email_unset(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "ADMIN_EMAIL", "")
    assert notify.notify_admin("x", {"a": "b"}) is False
```

## `.env.example`

```text
# ElevenLabs, used for Georgian speech to text (Scribe)
ELEVENLABS_API_KEY=

# Anthropic, used for structured summarisation
ANTHROPIC_API_KEY=

# Gmail account used to send the summary
# Use a Google App Password, not the account password
EMAIL_SENDER=
EMAIL_PASSWORD=

# Branding shown in the summary email
ORG_NAME=
ORG_CONTACT_EMAIL=

# Web interface
# Required. Without it the upload page refuses every request.
APP_PASSCODE=

# Get an email every time someone runs a summary. Leave empty for no notifications.
ADMIN_EMAIL=

# Optional limits
MAX_UPLOAD_MB=25
MAX_JOBS_PER_HOUR=5
# Restrict who summaries can be sent to, comma separated. Empty means anyone.
ALLOWED_RECIPIENT_DOMAINS=
```

## `README.md`

````markdown
# Meeting Summarizer (Georgian)

Turns a recorded meeting into a structured summary and emails it to the attendees. Built for Georgian-language meetings, which most off-the-shelf meeting tools do not transcribe usably.

Audio or video in, and out comes a titled summary with decisions, action items with an owner and a deadline, and a list of unresolved questions, delivered as a formatted HTML email with the full transcript collapsed underneath.

Two ways in: a web page where you upload a recording and type the recipients, or a command line for scripted use.

## Why it exists

Meeting notes were being written by hand after every offline meeting and emailed round afterwards. The commercial tools that automate this are built for online calls in English. This one is built for the opposite case: a recording made in the room, in Georgian, summarised into the structure a business meeting actually produces.

## How it works

```
upload page  or  command line
        |
ElevenLabs Scribe v2, language_code = kat
diarize on, verbatim off                      ->  transcripts/
        |
Claude, system prompt tuned for Georgian
business meetings, returns strict JSON        ->  summaries/
        |
HTML email via Gmail SMTP                     ->  attendees
```

Three modules, one entry point:

| File | Responsibility |
|---|---|
| `run.py` | Command line entry point, orchestrates the three steps, saves intermediate output |
| `src/transcribe.py` | Speech to text through ElevenLabs Scribe v2, Georgian, with speaker labels |
| `src/summarize.py` | Summarisation through Claude, returns validated JSON |
| `src/mailer.py` | Builds the HTML email and sends it over Gmail SMTP |
| `src/notify.py` | Optional usage notification to the operator |
| `app.py` | Web interface: upload, validation, background job, status polling |
| `templates/index.html` | The upload page, Georgian and English |

## The part that took the work

Transcription alone is not useful. A raw Georgian transcript of a 40 minute meeting is still 40 minutes of reading. The value is in the extraction, and naive summarisation invents decisions that were never made.

Three things address that. The system prompt keys on the phrases Georgian speakers actually use to mark a decision, an assignment, or an open question, so extraction is anchored to signal words rather than to the model's guess about what mattered. The prompt is explicitly instructed to omit rather than guess: unclear items get flagged as needing clarification, names that cannot be heard clearly become a generic participant label, and numbers are only reported when stated explicitly. And transcription runs with diarization on and verbatim off, so the text reaching Claude is speaker-labelled and free of filler words, which is what makes it possible to attribute an action item to whoever committed to it.

A summary that says "this was unclear" is worth more than a confident invention.

## Setup

Requires Python 3.11 or later.

```bash
git clone https://github.com/sophiaaistudies-tech/Meeting-Summarizer.git
cd Meeting-Summarizer
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in the keys
```

`EMAIL_PASSWORD` must be a Google App Password, not the account password. Gmail rejects the account password over SMTP.

## Usage

### Web interface

```bash
uvicorn app:app --port 8000
```

Open http://localhost:8000, choose a recording, enter the recipients and the passcode, and the summary is emailed when the three stages finish.

`APP_PASSCODE` must be set. Mail leaves your own Gmail account, so an open upload page would let anyone send mail from your address to anyone. With no passcode configured the app refuses every upload rather than running open.

Other limits, all configurable in `.env`:

| Variable | Default | What it does |
|---|---|---|
| `APP_PASSCODE` | none | Required. Users enter this to upload |
| `MAX_UPLOAD_MB` | 25 | Uploads are streamed and cut off above this |
| `MAX_JOBS_PER_HOUR` | 5 | Per IP address |
| `ALLOWED_RECIPIENT_DOMAINS` | empty | Restricts recipients, for example `gah.ge,gmail.com` |
| `ADMIN_EMAIL` | empty | Gets a short email after every run, worked or failed |

Recordings are written to a temporary file, processed, and deleted. Nothing is stored on the server.

### Knowing when it is used

Set `ADMIN_EMAIL` and every run sends you a one-screen summary: whether it worked, the meeting title or the error, who it was emailed to, the file name and size, the IP it came from, and how long it took. Notifications are wrapped so that a failure to send one can never fail the summary it is reporting on.

### Command line

```bash
python run.py recordings/meeting.m4a --to person@example.com,other@example.com
```

The transcript and the JSON summary are written to `transcripts/` and `summaries/`, timestamped, before the email is sent, so a failed send never loses the work.

## Output shape

```json
{
  "title": "short meeting title",
  "summary": "two or three sentences, confirmed facts only",
  "decisions": ["decisions that were explicitly stated"],
  "action_items": [{"task": "...", "owner": "...", "deadline": "..."}],
  "unresolved": ["questions left open"]
}
```

Summary text is generated in Georgian script.

## Limitations, honestly

- Diarization separates speakers but does not name them. Output is `speaker_0` and `speaker_1` unless someone is addressed by name in the recording.
- **Microphone distance is the variable that matters most.** A phone close to the speaker transcribes cleanly. The same conversation recorded across a room degrades badly, and no setting compensates for it.
- Overlapping speech reduces both transcription and diarization accuracy.
- Gmail SMTP only. Any other provider needs a change in `src/mailer.py`.
- Recordings, transcripts and summaries stay on the machine that runs it and are deliberately excluded from version control.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The three pipeline stages are replaced with fakes, so the suite runs without API keys and sends no mail. It covers the happy path, passcode enforcement, the fail-closed behaviour when no passcode is configured, recipient and file validation, the size ceiling, rate limiting, temporary file cleanup, how a failure in any stage is reported back to the page, and that usage notifications fire on both outcomes without ever being able to break a job.

## Deploying

Any host that runs a Python web process works. There is a `Procfile` for Railway and similar platforms:

```
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set every variable from `.env.example` in the host's environment. The filesystem can be ephemeral, since uploads are temporary by design.

## Stack

Python, FastAPI, ElevenLabs Scribe v2, Anthropic Claude, Gmail SMTP.
````
