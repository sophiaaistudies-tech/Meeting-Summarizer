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
