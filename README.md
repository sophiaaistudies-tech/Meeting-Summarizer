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
| `TRUST_PROXY` | empty | Set to `1` behind a proxy so rate limiting sees the real visitor |

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

Set `TRUST_PROXY=1` on any host that terminates TLS in front of the app. Without it the app sees the proxy's address rather than the visitor's, so every user shares a single rate-limit allowance and notifications report a useless IP. It defaults off because the `X-Forwarded-For` header is trivially forged when an app is reachable directly.

A public URL changes the threat model. Use a passcode that is long and not derived from anything printed in the summary email, and consider setting `ALLOWED_RECIPIENT_DOMAINS` so the page cannot be used to mail strangers from your account.

## Stack

Python, FastAPI, ElevenLabs Scribe v2, Anthropic Claude, Gmail SMTP.

## Licence

MIT. See `LICENSE`.
