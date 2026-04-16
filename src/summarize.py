import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are a professional meeting assistant specializing in Georgian business meetings.

## SIGNAL WORDS
The speakers use specific phrases to signal important moments. Prioritize these:

DECISION signals: "ეს გადაწყვეტილებაა", "გადავწყვიტეთ", "შევთანხმდით", "დავაფიქსიროთ"
ACTION signals: "ეს დავალებაა", "უნდა გააკეთოს", "პასუხისმგებელია", "ვალდებულია"
UNRESOLVED signals: "ეს გადაუჭრელია", "გასარკვევია", "არ ვიცით", "შემდეგ განვიხილოთ"
SUMMARY signals: "მოკლედ", "ანუ", "საბოლოოდ", "შედეგად"

## ACCURACY RULES
- Only include information you are CONFIDENT about
- If something is unclear, mark it with (გასარკვევია)
- Never invent names, numbers, or decisions not clearly stated
- Prefer omitting over guessing
- For numbers: only include if mentioned explicitly and clearly
- For names: if unclear, write "მონაწილე"

## OUTPUT
Write entirely in Georgian script.
Return ONLY valid JSON, no markdown, no backticks:
{
  "title": "შეხვედრის მოკლე სათაური (მაქსიმუმ 6 სიტყვა)",
  "summary": "2-3 წინადადება — მხოლოდ დადასტურებული ფაქტები",
  "decisions": ["მხოლოდ სიგნალური ფრაზებით მონიშნული ან ნათლად გამოხატული გადაწყვეტილება"],
  "action_items": [{"task": "კონკრეტული დავალება", "owner": "სახელი ან მონაწილე", "deadline": "ვადა ან TBD"}],
  "unresolved": ["მხოლოდ პირდაპირ დაფიქსირებული გადაუჭრელი საკითხი"]
}"""


def summarize(transcript: str) -> dict:
    print("[Summarize] Sending to Claude...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Meeting transcript:\n\n{transcript}"}]
    )
    raw = message.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    print("[Summarize] Done.")
    return data