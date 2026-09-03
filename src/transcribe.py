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
