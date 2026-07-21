import os
from faster_whisper import WhisperModel

model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "large-v3")
model = WhisperModel(model_path, device="cuda", compute_type="float16")

segments, info = model.transcribe("audio/sinica_tooze.mp3", language="en")

print(f"检测到语言: {info.language}, 概率: {info.language_probability:.2f}")

with open("output/sinica_tooze.txt", "w", encoding="utf-8") as f:
    for segment in segments:
        line = f"[{segment.start:.1f}s -> {segment.end:.1f}s] {segment.text}"
        print(line)
        f.write(line + "\n")