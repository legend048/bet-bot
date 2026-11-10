import logging
import os
import requests
from pathlib import Path
from groq import Groq

logger = logging.getLogger("BharatBot")

class Transcriber:
    def __init__(self, config: dict):
        ai_cfg = config.get("ai", {})
        self.model = ai_cfg.get("stt_model", "whisper-large-v3-turbo")
        self.temp_dir = config.get("paths", {}).get("temp_audio_dir", "./temp")
        Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        logger.info(f"Transcriber ready. Model: {self.model}")

    def download_audio(self, url: str, filename: str = "listening_audio.mp3") -> str | None:
        local_path = str(Path(self.temp_dir) / filename)
        try:
            response = requests.get(url, timeout=30, stream=True)
            response.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192): f.write(chunk)
            return local_path
        except Exception as e:
            logger.error(f"Failed to download audio: {e}")
            return None

    def transcribe(self, audio_file_path: str) -> str:
        """Transcribes audio file, chunking it if it's too large."""
        try:
            file_size_mb = os.path.getsize(audio_file_path) / (1024 * 1024)
            # Groq limit is 25MB, let's use 20MB as safety
            if file_size_mb > 20:
                logger.info(f"File size {file_size_mb:.2f}MB exceeds limit. Chunking...")
                return self._transcribe_chunks(audio_file_path)

            return self._execute_transcription(audio_file_path)
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return ""

    def _execute_transcription(self, audio_file_path: str) -> str:
        """Internal method to call Groq API for transcription."""
        with open(audio_file_path, "rb") as audio_file:
            transcription = self.client.audio.transcriptions.create(
                file=(Path(audio_file_path).name, audio_file.read()),
                model=self.model,
                language="en",
                response_format="text",
                temperature=0.0
            )
        return transcription.strip()

    def _transcribe_chunks(self, audio_file_path: str) -> str:
        """Splits audio into 10-minute chunks and transcribes each."""
        try:
            import subprocess
            import shutil
            if not shutil.which("ffmpeg"):
                raise EnvironmentError("ffmpeg is not installed or not in PATH. Required for audio chunking.")

            output_pattern = str(Path(self.temp_dir) / "chunk_%03d.mp3")
            # Split into 10 minute (600s) chunks
            cmd = [
                "ffmpeg", "-i", audio_file_path,
                "-f", "segment", "-segment_time", "600",
                "-c", "copy", output_pattern
            ]
            subprocess.run(cmd, check=True, capture_output=True)

            chunks = sorted(Path(self.temp_dir).glob("chunk_*.mp3"))
            full_transcript = []

            for chunk in chunks:
                logger.info(f"Transcribing chunk: {chunk.name}")
                chunk_text = self._execute_transcription(str(chunk))
                if chunk_text:
                    full_transcript.append(chunk_text)
                chunk.unlink() # Delete chunk after processing

            return " ".join(full_transcript)
        except Exception as e:
            logger.error(f"Chunked transcription failed: {e}")
            return ""

    def download_and_transcribe(self, audio_url: str) -> str:
        local_path = self.download_audio(audio_url)
        return self.transcribe(local_path) if local_path else ""

    def cleanup(self):
        for f in Path(self.temp_dir).glob("*.mp3"): f.unlink()
        for f in Path(self.temp_dir).glob("*.wav"): f.unlink()
