import os
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

load_dotenv()

client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

def transcribe(audio_path: str) -> str:
    print(f"[Transcribe] Sending to ElevenLabs Scribe v2: {audio_path}")
    with open(audio_path, "rb") as f:
        result = client.speech_to_text.convert(
            file=f,
            model_id="scribe_v2",
            language_code="kat"
        )
    text = result.text.strip()
    print(f"[Transcribe] Done. {len(text)} characters.")
    return text