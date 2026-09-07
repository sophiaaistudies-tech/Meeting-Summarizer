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
