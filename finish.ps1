# =====================================================================
#  Meeting-Summarizer: finish and publish
# =====================================================================
#  Put this ONE file in your project folder:
#      C:\Users\Mylaptop\meeting-summarizer
#  Then open PowerShell there and run:
#      powershell -ExecutionPolicy Bypass -File .\finish.ps1
#
#  It writes every corrected file, checks your keys, optionally runs the
#  full pipeline once, then commits and pushes to GitHub.
#  Nothing is deleted from your disk.
# =====================================================================

$ErrorActionPreference = "Stop"

function Write-Utf8($path, $content) {
    $dir = Split-Path -Parent $path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $path), $content, $utf8)
    Write-Host "  written  $path"
}

function Step($n, $text) {
    Write-Host ""
    Write-Host "[$n] $text" -ForegroundColor Cyan
}

if (-not (Test-Path ".git")) {
    Write-Host "This is not a git repository." -ForegroundColor Red
    Write-Host "Move finish.ps1 into C:\Users\Mylaptop\meeting-summarizer and run it there." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Meeting-Summarizer: finish and publish" -ForegroundColor Green
Write-Host "======================================"

Step 1 "Writing corrected files"

$f_README_md = @'
# Meeting Summarizer (Georgian)

Turns a recorded meeting into a structured summary and emails it to the attendees. Built for Georgian-language meetings, which most off-the-shelf meeting tools do not transcribe usably.

Audio or video in, and out comes a titled summary with decisions, action items with an owner and a deadline, and a list of unresolved questions, delivered as a formatted HTML email with the full transcript collapsed underneath.

## Why it exists

Meeting notes were being written by hand after every offline meeting and emailed round afterwards. The commercial tools that automate this are built for online calls in English. This one is built for the opposite case: a recording made in the room, in Georgian, summarised into the structure a business meeting actually produces.

## How it works

```
audio or video file
        |
ElevenLabs Scribe v2, language_code = kat     ->  transcripts/
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

## Stack

Python, ElevenLabs Scribe v2, Anthropic Claude, Gmail SMTP.

'@
Write-Utf8 "README.md" $f_README_md

$f_requirements_txt = @'
elevenlabs
anthropic
python-dotenv

'@
Write-Utf8 "requirements.txt" $f_requirements_txt

$f__gitignore = @'
# Secrets
.env

# Python
__pycache__/
*.py[cod]
.venv/
venv/

# Local meeting data, never commit
recordings/
transcripts/
summaries/

# Editor
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db

'@
Write-Utf8 ".gitignore" $f__gitignore

$f__env_example = @'
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

'@
Write-Utf8 ".env.example" $f__env_example

$f_src_transcribe_py = @'
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

'@
Write-Utf8 "src/transcribe.py" $f_src_transcribe_py

$f_src_mailer_py = @'
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
'@
Write-Utf8 "src/mailer.py" $f_src_mailer_py

$f_test_transcribe_py = @'
"""
Georgian transcription diagnostic.

Runs the same audio file through four combinations of Scribe model and
language code, prints the first 300 characters of each, and saves the full
output so you can compare.

Usage:
    python test_transcribe.py recordings/yourfile.wav

Use a short clip, 30 to 60 seconds, of clear Georgian speech with one speaker
close to the microphone. A long noisy recording tells you nothing about which
setting is at fault.

Requires ELEVENLABS_API_KEY in .env.
"""

import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

API_KEY = os.getenv("ELEVENLABS_API_KEY")
if not API_KEY:
    print("ELEVENLABS_API_KEY is not set. Add it to your .env file.")
    sys.exit(1)

client = ElevenLabs(api_key=API_KEY)

# model_id, language_code, why this combination is worth testing
COMBINATIONS = [
    ("scribe_v2", "kat", "current setting in src/transcribe.py, ISO-639-3"),
    ("scribe_v2", "ka",  "same model, ISO-639-1 code instead"),
    ("scribe_v1", "kat", "older model, which has documented 99-language support"),
    ("scribe_v2", None,  "no language hint, let the model detect it"),
]

GEORGIAN_RANGE = range(0x10A0, 0x1100)


def georgian_share(text):
    """Rough sanity check: how much of the output is actually Georgian script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    georgian = [c for c in letters if ord(c) in GEORGIAN_RANGE]
    return len(georgian) / len(letters)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    audio_path = sys.argv[1]
    if not os.path.exists(audio_path):
        print(f"File not found: {audio_path}")
        sys.exit(1)

    outdir = "diagnostic"
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")

    print(f"\nFile: {audio_path}")
    print(f"Testing {len(COMBINATIONS)} combinations.\n")

    for model_id, lang, note in COMBINATIONS:
        label = f"{model_id} / {lang or 'auto-detect'}"
        print("=" * 70)
        print(f"{label}")
        print(f"({note})")
        print("=" * 70)

        try:
            kwargs = {"file": None, "model_id": model_id}
            with open(audio_path, "rb") as f:
                kwargs["file"] = f
                if lang is not None:
                    kwargs["language_code"] = lang
                result = client.speech_to_text.convert(**kwargs)

            text = (result.text or "").strip()
            detected = getattr(result, "language_code", None)
            share = georgian_share(text)

            print(f"characters      : {len(text)}")
            print(f"detected lang   : {detected}")
            print(f"Georgian script : {share:.0%} of letters")
            print(f"first 300 chars :\n{text[:300]}\n")

            safe = f"{model_id}_{lang or 'auto'}"
            path = os.path.join(outdir, f"{safe}_{stamp}.txt")
            with open(path, "w", encoding="utf-8") as out:
                out.write(text)
            print(f"saved           : {path}\n")

        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}\n")

    print("=" * 70)
    print("How to read this:")
    print("  If one combination returns readable Georgian and the others do not,")
    print("  change src/transcribe.py to that combination and you are done.")
    print("  If all four return nonsense, the problem is the audio, not the")
    print("  settings. Re-record 30 seconds close to the microphone and rerun.")
    print("  If all four fail with an error, read the error text, it will name")
    print("  the cause (invalid model, quota, or key).")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()

'@
Write-Utf8 "test_transcribe.py" $f_test_transcribe_py


Step 2 "Checking .gitignore is readable by git"
$bytes = [System.IO.File]::ReadAllBytes((Join-Path (Get-Location) ".gitignore"))
if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
    Write-Host "  .gitignore is still UTF-16. Stopping." -ForegroundColor Red
    exit 1
}
Write-Host "  ok, plain UTF-8"

Step 3 "Checking your .env keys"
if (-not (Test-Path ".env")) {
    Write-Host "  .env is missing. Copy .env.example to .env and fill in your keys, then rerun." -ForegroundColor Red
    exit 1
}
$envText = Get-Content ".env" -Raw
$missing = @()
foreach ($k in @("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "EMAIL_SENDER", "EMAIL_PASSWORD")) {
    if ($envText -notmatch "(?m)^\s*$k\s*=\s*\S") { $missing += $k }
}
if ($missing.Count -gt 0) {
    Write-Host "  These are empty or missing in .env:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host "  Fill them in and rerun." -ForegroundColor Red
    exit 1
}
Write-Host "  ok, all four keys are set"
if ($envText -match "(?m)^\s*OPENAI_API_KEY") {
    Write-Host "  note: OPENAI_API_KEY is still in .env. Nothing uses it, you can delete that line." -ForegroundColor Yellow
}
if ($envText -notmatch "(?m)^\s*ORG_NAME\s*=\s*\S") {
    Write-Host "  note: ORG_NAME is empty, so the email header will have no company name." -ForegroundColor Yellow
}

Step 4 "Installing dependencies"
pip install -q -r requirements.txt
Write-Host "  ok"

Step 5 "Optional: run the full pipeline once to prove it works"
Write-Host "  This transcribes a recording, summarises it, and emails the result."
$run = Read-Host "  Run it now? (y/n)"
if ($run -eq "y") {
    Write-Host ""
    Write-Host "  Files in recordings:" -ForegroundColor Yellow
    Get-ChildItem recordings -File | ForEach-Object { Write-Host "    $($_.Name)" }
    $clip = Read-Host "  Filename to use"
    $to   = Read-Host "  Email address to send the summary to"
    Write-Host ""
    python run.py "recordings/$clip" --to $to
    Write-Host ""
    $ok = Read-Host "  Did the email arrive and look right? (y/n)"
    if ($ok -ne "y") {
        Write-Host "  Stopping before publishing. Paste the output above to Claude." -ForegroundColor Yellow
        exit 1
    }
}

Step 6 "Removing test data, caches and editor settings from git"
foreach ($p in @("recordings", "transcripts", "summaries", "diagnostic", "src/__pycache__", ".vscode")) {
    $tracked = git ls-files $p
    if ($tracked) {
        git rm -r --cached -q $p
        Write-Host "  untracked  $p  (still on your disk)"
    }
}

Step 7 "Committing"
git add -A
git commit -m "Working Georgian meeting summarizer: diarization, filler removal, real README, correct dependencies, no test data"

Step 8 "Publishing to GitHub"
git push
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Push failed. Copy the error above and send it to Claude." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Published." -ForegroundColor Green
Write-Host "Open https://github.com/sophiaaistudies-tech/Meeting-Summarizer and check:"
Write-Host "  1. The README shows headings and a pipeline diagram"
Write-Host "  2. Only .env.example, .gitignore, README.md, requirements.txt,"
Write-Host "     run.py, test_transcribe.py and the src folder are listed"
Write-Host "  3. requirements.txt has exactly three lines"
Write-Host ""
