# BUILD.md

Everything needed to add the web interface to this project is in this one file.

## How to use it

1. Save this file into `C:\Users\Mylaptop\meeting-summarizer` as `BUILD.md`
2. Open PowerShell there and run `claude`
3. Send this one message:

   > Read BUILD.md in this folder and carry out the instructions in it.

Nothing else to download.

---

## Instructions for Claude Code

Work through these in order. Stop and tell the user if a step fails, rather than
continuing.

**1. Create the files.** Every file listed below the horizontal rule is given with
its path as a heading and its full contents in a code block. Write each one exactly
as given, creating folders as needed, overwriting what is already there.

Two files must not be touched: `src/summarize.py` and `.env`.

Note on encoding: several files contain Georgian text. Write every file as UTF-8
without a byte order mark. The old `.gitignore` in this repository was UTF-16, which
git could not read, and that is what allowed `.env` to be committed once already.

**2. Extend `.env`.** Add any of these keys that are missing, leaving existing
values untouched:

```
APP_PASSCODE=
MAX_UPLOAD_MB=25
MAX_JOBS_PER_HOUR=5
ALLOWED_RECIPIENT_DOMAINS=
ORG_NAME=
ORG_CONTACT_EMAIL=
```

Then ask the user what `APP_PASSCODE` and `ORG_NAME` should be and write the answers
in. Do not invent a passcode. The app refuses all uploads when `APP_PASSCODE` is
empty, which is deliberate: mail is sent from the user's own Gmail account, so an
unprotected page would let anyone send mail from their address.

**3. Install.** `pip install -r requirements-dev.txt`

**4. Test.** `pytest -q` must report 22 passed. If anything fails, show the output
and fix it before going further.

**5. Try it for real.** Start `uvicorn app:app --port 8000`, leave it running, and
ask the user to open http://localhost:8000, upload `recordings/test_clean.mp4`,
enter their email and passcode, and say whether the summary email arrived. Wait for
their answer.

**6. Publish, only after they confirm.**
- `git rm -r --cached recordings transcripts summaries diagnostic` (keep the files on disk)
- verify `.env` is not staged, and stop if it is
- commit: `Add web interface for uploads with passcode, limits and tests`
- push
- show `git ls-files` so the user can confirm nothing private went public

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


def _process(job_id: str, audio_path: str, recipients: list):
    """Runs in a background thread. Every stage failure is reported to the UI."""
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
    except Exception as e:
        _set(job_id, stage="failed", error=f"{type(e).__name__}: {e}")
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


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
    background_tasks.add_task(_process, job_id, tmp_path, to)
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

## `templates/index.html`

```html
<!DOCTYPE html>
<html lang="ka">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>შეხვედრის შეჯამება</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian:wght@400;500;600&family=Noto+Serif+Georgian:wght@600&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#1A1A1A;
    --muted:#666666;
    --paper:#FAFAF8;
    --line:#E0E0E0;
    --green:#2D7A3A;
    --green-tint:#EBF5ED;
    --red:#A32222;
    --sans:'Noto Sans Georgian','Segoe UI',system-ui,sans-serif;
    --serif:'Noto Serif Georgian',Georgia,serif;
  }
  *{box-sizing:border-box}
  body{
    margin:0;background:var(--paper);color:var(--ink);
    font-family:var(--sans);font-size:16px;line-height:1.6;
    -webkit-font-smoothing:antialiased;
  }
  .sheet{max-width:37rem;margin:0 auto;padding:3rem 1.25rem 4rem}

  header{border-left:3px solid var(--green);padding-left:1rem;margin-bottom:2.5rem}
  h1{font-family:var(--serif);font-weight:600;font-size:1.75rem;line-height:1.25;margin:0 0 .4rem}
  .lede{margin:0;color:var(--muted);max-width:32rem}

  .lang{float:right;display:flex;gap:.25rem;margin-top:.2rem}
  .lang button{
    font-family:var(--sans);font-size:.8rem;background:none;border:1px solid var(--line);
    color:var(--muted);padding:.15rem .5rem;cursor:pointer;border-radius:2px;
  }
  .lang button[aria-pressed="true"]{border-color:var(--green);color:var(--green);background:var(--green-tint)}

  .field{border-top:1px solid var(--line);padding:1.5rem 0}
  .field:last-of-type{border-bottom:1px solid var(--line)}
  label{display:block;font-weight:600;font-size:.95rem;margin-bottom:.35rem}
  .hint{color:var(--muted);font-size:.85rem;margin:0 0 .75rem}

  .drop{
    display:block;width:100%;text-align:left;cursor:pointer;
    background:#fff;border:1px solid var(--line);border-left:3px solid var(--line);
    padding:1.1rem 1rem;font-family:var(--sans);font-size:.95rem;color:var(--muted);
  }
  .drop:hover,.drop.over{border-left-color:var(--green);color:var(--ink)}
  .drop.has-file{border-left-color:var(--green);color:var(--ink);background:var(--green-tint)}
  .drop .fname{font-weight:600;display:block}
  .drop .fmeta{font-size:.8rem;color:var(--muted)}
  input[type=file]{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}

  input[type=text],input[type=password],textarea{
    width:100%;font-family:var(--sans);font-size:1rem;color:var(--ink);
    background:#fff;border:1px solid var(--line);padding:.7rem .8rem;border-radius:2px;
  }
  textarea{resize:vertical;min-height:4.5rem}
  :focus-visible{outline:2px solid var(--green);outline-offset:2px}

  .go{
    margin-top:1.75rem;width:100%;font-family:var(--sans);font-weight:600;font-size:1rem;
    background:var(--green);color:#fff;border:none;padding:.9rem 1rem;cursor:pointer;border-radius:2px;
  }
  .go:disabled{background:#B7C9BB;cursor:default}

  .stages{margin-top:2rem;display:none}
  .stages.on{display:block}
  .stage{display:flex;gap:.75rem;align-items:baseline;padding:.45rem 0;color:var(--muted)}
  .stage .n{
    flex:none;width:1.4rem;height:1.4rem;border:1px solid var(--line);border-radius:50%;
    font-size:.75rem;display:grid;place-items:center;color:var(--muted);background:#fff;
  }
  .stage.active{color:var(--ink);font-weight:600}
  .stage.active .n{border-color:var(--green);color:var(--green)}
  .stage.done .n{background:var(--green);border-color:var(--green);color:#fff}
  .stage.done{color:var(--ink)}

  .result{margin-top:1.5rem;padding:1rem;border-left:3px solid var(--green);background:var(--green-tint);display:none}
  .result.on{display:block}
  .result h2{font-family:var(--serif);font-size:1.05rem;margin:0 0 .3rem}
  .result p{margin:0;font-size:.9rem;color:var(--muted)}
  .error{margin-top:1.5rem;padding:1rem;border-left:3px solid var(--red);background:#FBF0F0;display:none}
  .error.on{display:block}
  .error p{margin:0;font-size:.9rem}
  .error code{font-size:.8rem;color:var(--muted);word-break:break-all}

  footer{margin-top:3rem;color:var(--muted);font-size:.85rem}
  footer p{margin:.4rem 0}

  @media (prefers-reduced-motion: no-preference){
    .stage,.drop,.go{transition:color .15s,border-color .15s,background .15s}
  }
</style>
</head>
<body>
<main class="sheet">
  <div class="lang">
    <button type="button" data-lang="ka" aria-pressed="true">ქარ</button>
    <button type="button" data-lang="en" aria-pressed="false">EN</button>
  </div>

  <header>
    <h1 data-t="title">შეხვედრის შეჯამება</h1>
    <p class="lede" data-t="lede">ატვირთე შეხვედრის ჩანაწერი. ტექსტი ამოიშლება, შეჯამდება გადაწყვეტილებებად და დავალებებად, და მიდის მითითებულ მისამართებზე.</p>
  </header>

  <div class="field">
    <label for="file" data-t="fileLabel">ჩანაწერი</label>
    <p class="hint" data-t="fileHint">აუდიო ან ვიდეო. მიკროფონი ახლოს იყოს მოსაუბრესთან, სიზუსტე ამაზე დგას.</p>
    <button type="button" class="drop" id="drop">
      <span data-t="dropEmpty">ფაილის ასარჩევად დააჭირე, ან ჩააგდე აქ</span>
    </button>
    <input type="file" id="file" accept="audio/*,video/*">
  </div>

  <div class="field">
    <label for="recipients" data-t="toLabel">მიმღებები</label>
    <p class="hint" data-t="toHint">მეილები მძიმით, მაქსიმუმ ხუთი.</p>
    <textarea id="recipients" placeholder="name@company.ge, name2@company.ge"></textarea>
  </div>

  <div class="field">
    <label for="passcode" data-t="codeLabel">კოდი</label>
    <p class="hint" data-t="codeHint">გაგზავნა ხდება კომპანიის მეილიდან, ამიტომ წვდომა კოდით იხსნება.</p>
    <input type="password" id="passcode" autocomplete="current-password">
  </div>

  <button class="go" id="go" data-t="go">შეჯამების გაგზავნა</button>

  <div class="stages" id="stages">
    <div class="stage" data-stage="transcribing"><span class="n">1</span><span data-t="s1">ტექსტად გადაყვანა</span></div>
    <div class="stage" data-stage="summarizing"><span class="n">2</span><span data-t="s2">შეჯამება</span></div>
    <div class="stage" data-stage="sending"><span class="n">3</span><span data-t="s3">გაგზავნა</span></div>
  </div>

  <div class="result" id="result">
    <h2 id="resultTitle"></h2>
    <p id="resultTo"></p>
  </div>

  <div class="error" id="error">
    <p data-t="errLead">ვერ დასრულდა.</p>
    <p><code id="errText"></code></p>
  </div>

  <footer>
    <p data-t="foot1">ჩანაწერი სერვერზე არ ინახება, დამუშავების შემდეგ იშლება.</p>
    <p data-t="foot2">ქართული, ავტომატური ენის ამოცნობით და მოსაუბრეების გამოყოფით.</p>
  </footer>
</main>

<script>
const T = {
  ka:{
    title:"შეხვედრის შეჯამება",
    lede:"ატვირთე შეხვედრის ჩანაწერი. ტექსტი ამოიშლება, შეჯამდება გადაწყვეტილებებად და დავალებებად, და მიდის მითითებულ მისამართებზე.",
    fileLabel:"ჩანაწერი",
    fileHint:"აუდიო ან ვიდეო. მიკროფონი ახლოს იყოს მოსაუბრესთან, სიზუსტე ამაზე დგას.",
    dropEmpty:"ფაილის ასარჩევად დააჭირე, ან ჩააგდე აქ",
    toLabel:"მიმღებები", toHint:"მეილები მძიმით, მაქსიმუმ ხუთი.",
    codeLabel:"კოდი", codeHint:"გაგზავნა ხდება კომპანიის მეილიდან, ამიტომ წვდომა კოდით იხსნება.",
    go:"შეჯამების გაგზავნა", going:"მუშაობს...",
    s1:"ტექსტად გადაყვანა", s2:"შეჯამება", s3:"გაგზავნა",
    errLead:"ვერ დასრულდა.",
    sent:"გაგზავნილია", sentTo:"მიმღებები: ",
    foot1:"ჩანაწერი სერვერზე არ ინახება, დამუშავების შემდეგ იშლება.",
    foot2:"ქართული, ავტომატური ენის ამოცნობით და მოსაუბრეების გამოყოფით.",
    needFile:"აირჩიე ჩანაწერი.", needTo:"მიუთითე მინიმუმ ერთი მეილი.", needCode:"შეიყვანე კოდი."
  },
  en:{
    title:"Meeting summary",
    lede:"Upload a recording. It is transcribed, summarised into decisions and action items, and emailed to the addresses you enter.",
    fileLabel:"Recording",
    fileHint:"Audio or video. Keep the microphone close to whoever is speaking, accuracy depends on it.",
    dropEmpty:"Click to choose a file, or drop one here",
    toLabel:"Recipients", toHint:"Email addresses separated by commas, five at most.",
    codeLabel:"Passcode", codeHint:"Mail is sent from the company account, so access needs a passcode.",
    go:"Send the summary", going:"Working...",
    s1:"Transcribing", s2:"Summarising", s3:"Sending",
    errLead:"It did not finish.",
    sent:"Sent", sentTo:"Recipients: ",
    foot1:"The recording is not kept on the server, it is deleted after processing.",
    foot2:"Georgian, with automatic language detection and speaker separation.",
    needFile:"Choose a recording.", needTo:"Enter at least one email address.", needCode:"Enter the passcode."
  }
};
let lang = "ka";

const $ = id => document.getElementById(id);
const fileInput = $("file"), drop = $("drop"), go = $("go"),
      stages = $("stages"), result = $("result"), errBox = $("error");

function paint(){
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-t]").forEach(el=>{
    const v = T[lang][el.dataset.t];
    if(v) el.textContent = v;
  });
  document.querySelectorAll(".lang button").forEach(b=>
    b.setAttribute("aria-pressed", String(b.dataset.lang===lang)));
  if(fileInput.files[0]) showFile(fileInput.files[0]);
  go.textContent = go.disabled ? T[lang].going : T[lang].go;
}
document.querySelectorAll(".lang button").forEach(b=>
  b.addEventListener("click",()=>{ lang=b.dataset.lang; paint(); }));

function showFile(f){
  const mb = (f.size/1048576).toFixed(1);
  drop.innerHTML = "";
  const n = document.createElement("span"); n.className="fname"; n.textContent=f.name;
  const m = document.createElement("span"); m.className="fmeta"; m.textContent=mb+" MB";
  drop.append(n,m); drop.classList.add("has-file");
}
drop.addEventListener("click",()=>fileInput.click());
fileInput.addEventListener("change",()=>{ if(fileInput.files[0]) showFile(fileInput.files[0]); });
["dragenter","dragover"].forEach(e=>drop.addEventListener(e,ev=>{
  ev.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(e=>drop.addEventListener(e,ev=>{
  ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop",ev=>{
  const f = ev.dataTransfer.files[0];
  if(f){ fileInput.files = ev.dataTransfer.files; showFile(f); }
});

function fail(msg){
  errBox.classList.add("on");
  $("errText").textContent = msg;
  go.disabled = false; go.textContent = T[lang].go;
}
function markStage(current){
  const order = ["transcribing","summarizing","sending"];
  const i = order.indexOf(current);
  document.querySelectorAll(".stage").forEach((el,idx)=>{
    el.classList.toggle("active", idx===i);
    el.classList.toggle("done", i===-1 ? current==="done" : idx<i);
  });
}

go.addEventListener("click", async ()=>{
  errBox.classList.remove("on"); result.classList.remove("on");
  const f = fileInput.files[0];
  if(!f) return fail(T[lang].needFile);
  if(!$("recipients").value.trim()) return fail(T[lang].needTo);
  if(!$("passcode").value) return fail(T[lang].needCode);

  go.disabled = true; go.textContent = T[lang].going;
  stages.classList.add("on"); markStage("transcribing");

  const body = new FormData();
  body.append("file", f);
  body.append("recipients", $("recipients").value);
  body.append("passcode", $("passcode").value);

  let jobId;
  try{
    const r = await fetch("/api/jobs",{method:"POST",body});
    const d = await r.json().catch(()=>({}));
    if(!r.ok) return fail(d.detail || ("HTTP "+r.status));
    jobId = d.job_id;
  }catch(e){ return fail(String(e)); }

  const poll = setInterval(async ()=>{
    try{
      const r = await fetch("/api/jobs/"+jobId);
      const d = await r.json();
      markStage(d.stage);
      if(d.stage==="done"){
        clearInterval(poll);
        document.querySelectorAll(".stage").forEach(el=>el.classList.add("done"));
        $("resultTitle").textContent = d.title || T[lang].sent;
        $("resultTo").textContent = T[lang].sentTo + (d.recipients||[]).join(", ");
        result.classList.add("on");
        go.disabled = false; go.textContent = T[lang].go;
      }else if(d.stage==="failed"){
        clearInterval(poll); fail(d.error||"unknown");
      }
    }catch(e){ clearInterval(poll); fail(String(e)); }
  }, 2000);
});

paint();
</script>
</body>
</html>
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
```

## `src/transcribe.py`

```python
import os
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

load_dotenv()

client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

MODEL_ID = "scribe_v2"
LANGUAGE = "kat"


def _label_speakers(words) -> str:
    """
    Turn the word list into speaker-labelled lines.

    Diarization puts a speaker_id on each word. Consecutive words from the same
    speaker are joined into one line, and a new line starts whenever the
    speaker changes. Returns an empty string when the response carries no
    usable speaker information, so the caller can fall back to plain text.
    """
    if not words:
        return ""

    lines = []
    current_speaker = None
    current_words = []

    for w in words:
        text = getattr(w, "text", None)
        if text is None and isinstance(w, dict):
            text = w.get("text")
        if not text:
            continue

        # Skip spacing and audio-event entries, keep actual words
        wtype = getattr(w, "type", None) or (w.get("type") if isinstance(w, dict) else None)
        if wtype == "spacing":
            continue

        speaker = getattr(w, "speaker_id", None)
        if speaker is None and isinstance(w, dict):
            speaker = w.get("speaker_id")

        if speaker != current_speaker:
            if current_words:
                lines.append(f"{current_speaker}: {' '.join(current_words).strip()}")
            current_speaker = speaker
            current_words = []

        current_words.append(text.strip())

    if current_words:
        lines.append(f"{current_speaker}: {' '.join(current_words).strip()}")

    # If every word came back without a speaker id, labelling adds nothing
    if len(lines) == 1 and lines[0].startswith("None: "):
        return ""

    return "\n".join(lines)


def transcribe(audio_path: str) -> str:
    print(f"[Transcribe] Sending to ElevenLabs {MODEL_ID}: {audio_path}")
    with open(audio_path, "rb") as f:
        result = client.speech_to_text.convert(
            file=f,
            model_id=MODEL_ID,
            language_code=LANGUAGE,
            diarize=True,        # who said what, needed for action item owners
            no_verbatim=True,    # drop filler words and false starts, scribe_v2 only
        )

    words = getattr(result, "words", None)
    labelled = _label_speakers(words)

    if labelled:
        speakers = len({line.split(":", 1)[0] for line in labelled.split("\n")})
        print(f"[Transcribe] Done. {len(labelled)} characters, {speakers} speaker(s).")
        return labelled

    text = (result.text or "").strip()
    print(f"[Transcribe] Done. {len(text)} characters, no speaker labels returned.")
    return text
```

## `src/mailer.py`

```python
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

EMAIL_SENDER   = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ORG_NAME       = os.getenv("ORG_NAME", "")
ORG_CONTACT    = os.getenv("ORG_CONTACT_EMAIL", "")

GREEN      = "#2D7A3A"
GREEN_LIGHT = "#EBF5ED"
GRAY       = "#F7F7F7"
DARK       = "#1A1A1A"
MUTED      = "#666666"
BORDER     = "#E0E0E0"


def format_html(data: dict, transcript: str) -> str:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = data.get("title", "შეხვედრის შეჯამება")

    def section(label, items, is_list=True):
        if not items:
            return ""
        rows = ""
        if is_list:
            for item in items:
                rows += f'<li style="margin-bottom:8px; color:{DARK};">{item}</li>'
            content = f'<ul style="margin:0; padding-left:20px;">{rows}</ul>'
        else:
            content = f'<p style="margin:0; color:{DARK}; line-height:1.7;">{items}</p>'
        return f"""
        <div style="margin-bottom:24px;">
            <div style="font-size:11px; font-weight:600; color:{GREEN}; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px; border-bottom:2px solid {GREEN}; 
                        padding-bottom:6px;">
                {label}
            </div>
            {content}
        </div>"""

    # Action items
    action_rows = ""
    for item in data.get("action_items", []):
        owner    = item.get("owner", "TBD")
        task     = item.get("task", "")
        deadline = item.get("deadline", "TBD")
        action_rows += f"""
        <tr>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{DARK}; font-size:14px;">{task}</td>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{MUTED}; font-size:13px; white-space:nowrap;">{owner}</td>
            <td style="padding:10px 12px; border-bottom:1px solid {BORDER}; 
                       color:{MUTED}; font-size:13px; white-space:nowrap;">{deadline}</td>
        </tr>"""

    action_section = ""
    if data.get("action_items"):
        action_section = f"""
        <div style="margin-bottom:24px;">
            <div style="font-size:11px; font-weight:600; color:{GREEN}; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px; border-bottom:2px solid {GREEN}; 
                        padding-bottom:6px;">
                დავალებები
            </div>
            <table style="width:100%; border-collapse:collapse; 
                          border:1px solid {BORDER}; border-radius:6px; 
                          overflow:hidden; font-size:14px;">
                <thead>
                    <tr style="background:{GREEN_LIGHT};">
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">დავალება</th>
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">პასუხისმგებელი</th>
                        <th style="padding:10px 12px; text-align:left; 
                                   color:{GREEN}; font-size:12px; 
                                   font-weight:600;">ვადა</th>
                    </tr>
                </thead>
                <tbody>{action_rows}</tbody>
            </table>
        </div>"""

    unresolved_section = ""
    if data.get("unresolved"):
        items_html = "".join([
            f'<li style="margin-bottom:8px; color:{DARK};">{u}</li>'
            for u in data.get("unresolved", [])
        ])
        unresolved_section = f"""
        <div style="margin-bottom:24px; background:#FFF8E7; 
                    border-left:4px solid #F0A500; 
                    border-radius:4px; padding:16px;">
            <div style="font-size:11px; font-weight:600; color:#B07800; 
                        text-transform:uppercase; letter-spacing:0.08em; 
                        margin-bottom:10px;">
                გადაუჭრელი საკითხები
            </div>
            <ul style="margin:0; padding-left:20px;">
                {items_html}
            </ul>
        </div>"""

    decisions = data.get("decisions", [])
    decisions_html = section("გადაწყვეტილებები", decisions)
    summary_html   = section("შეჯამება", data.get("summary", ""), is_list=False)

    transcript_lines = transcript.replace("\n", "<br>")

    footer_parts = [p for p in (ORG_NAME, ORG_CONTACT) if p]
    footer_org = (" · " + " · ".join(footer_parts)) if footer_parts else ""

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:{GRAY}; 
             font-family: Georgia, 'Times New Roman', serif;">

  <table width="100%" cellpadding="0" cellspacing="0" 
         style="background:{GRAY}; padding:32px 16px;">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" 
             style="background:#ffffff; border-radius:8px; 
                    overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:{GREEN}; padding:28px 36px;">
            <div style="font-size:13px; color:rgba(255,255,255,0.7); 
                        margin-bottom:4px; letter-spacing:0.05em;">
              {ORG_NAME}
            </div>
            <div style="font-size:22px; color:#ffffff; font-weight:600; 
                        line-height:1.3;">
              {title}
            </div>
            <div style="font-size:13px; color:rgba(255,255,255,0.7); 
                        margin-top:8px;">
              {now}
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 36px;">
            {summary_html}
            {decisions_html}
            {action_section}
            {unresolved_section}
          </td>
        </tr>

        <!-- Transcript -->
        <tr>
          <td style="padding:0 36px 32px;">
            <details>
              <summary style="cursor:pointer; color:{MUTED}; font-size:13px; 
                              padding:12px 16px; background:{GRAY}; 
                              border-radius:6px; user-select:none;">
                სრული ტრანსკრიფცია (დასაჭერია გასახსნელად)
              </summary>
              <div style="padding:16px; background:{GRAY}; border-radius:6px; 
                          margin-top:8px; font-size:13px; color:{MUTED}; 
                          line-height:1.8;">
                {transcript_lines}
              </div>
            </details>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:{GREEN_LIGHT}; padding:16px 36px; 
                     border-top:1px solid {BORDER};">
            <div style="font-size:12px; color:{MUTED}; text-align:center;">
              ეს შეჯამება გენერირებულია ავტომატურად AI-ის მეშვეობით
              {footer_org}
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>"""
    return html


def send(data: dict, transcript: str, recipients: list):
    print(f"[Email] Sending to {len(recipients)} recipient(s)...")
    title   = data.get("title", "შეხვედრის შეჯამება")
    date    = datetime.now().strftime("%Y-%m-%d")
    subject = f"შეხვედრა: {title} — {date}"

    html_body = format_html(data, transcript)

    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())

    print(f"[Email] Sent to: {', '.join(recipients)}")
```

## `requirements.txt`

```text
elevenlabs
anthropic
python-dotenv
fastapi
uvicorn[standard]
python-multipart
```

## `requirements-dev.txt`

```text
-r requirements.txt
pytest
httpx
```

## `Procfile`

```text
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

## `.gitignore`

```text
# Secrets
.env

# Python
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/

# Local meeting data, never commit
recordings/
transcripts/
summaries/
diagnostic/

# Editor
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
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

Recordings are written to a temporary file, processed, and deleted. Nothing is stored on the server.

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

The three pipeline stages are replaced with fakes, so the suite runs without API keys and sends no mail. It covers the happy path, passcode enforcement, the fail-closed behaviour when no passcode is configured, recipient and file validation, the size ceiling, rate limiting, temporary file cleanup, and how a failure in any stage is reported back to the page.

## Deploying

Any host that runs a Python web process works. There is a `Procfile` for Railway and similar platforms:

```
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set every variable from `.env.example` in the host's environment. The filesystem can be ephemeral, since uploads are temporary by design.

## Stack

Python, FastAPI, ElevenLabs Scribe v2, Anthropic Claude, Gmail SMTP.
````
