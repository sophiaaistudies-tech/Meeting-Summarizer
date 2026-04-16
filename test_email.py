import sys
import json
sys.path.insert(0, "src")
from summarize import summarize
from mailer import send

with open("transcripts\\newtest3_20260416_0233.txt", "r", encoding="utf-8") as f:
    transcript = f.read().strip()

print(f"[Input] {len(transcript)} characters loaded")
data = summarize(transcript)
send(data, transcript, ["soposhen@gmail.com"])
print("Done!")