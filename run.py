import os
import sys
import argparse
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from transcribe import transcribe
from summarize  import summarize
from mailer     import send

TRANSCRIPTS_DIR = "transcripts"
SUMMARIES_DIR   = "summaries"


def save_text(text, folder, prefix):
    os.makedirs(folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(folder, f"{prefix}_{timestamp}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[Saved] {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="Meeting Summarizer")
    parser.add_argument("audio", help="Path to audio/video file")
    parser.add_argument("--to", required=True, help="Comma-separated recipient emails")
    args = parser.parse_args()

    audio_path = args.audio
    recipients = [r.strip() for r in args.to.split(",")]

    if not os.path.exists(audio_path):
        print(f"Error: File not found — {audio_path}")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"Meeting Summarizer")
    print(f"File      : {audio_path}")
    print(f"Recipients: {', '.join(recipients)}")
    print(f"{'='*50}\n")

    # Step 1: Transcribe
    transcript = transcribe(audio_path)
    base_name  = os.path.splitext(os.path.basename(audio_path))[0]
    save_text(transcript, TRANSCRIPTS_DIR, base_name)

    # Step 2: Summarize
    data = summarize(transcript)

    # Step 3: Save summary
    summary_json = json.dumps(data, ensure_ascii=False, indent=2)
    save_text(summary_json, SUMMARIES_DIR, base_name)

    # Step 4: Send email
    send(data, transcript, recipients)

    print(f"\n{'='*50}")
    print(f"Done! Summary: {data.get('title','')}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()