import logging
import os
import wave
import subprocess
import shutil
import json
from pathlib import Path
from groq import Groq

logger = logging.getLogger("BharatBot")

class Speaker:
    def __init__(self, config: dict):
        ai_cfg = config.get("ai", {})
        self.model = ai_cfg.get("tts_model", "playai/tts")
        self.voice = ai_cfg.get("tts_voice", "Fritz-PlayAI")
        self.temp_dir = config.get("paths", {}).get("temp_audio_dir", "./temp")
        # The fake-microphone WAV path (Chrome reads this as the mic input).
        self.mic_file = config.get("browser", {}).get("virtual_mic_file", "./temp/mic_input.wav")
        Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
        Path(self.mic_file).parent.mkdir(parents=True, exist_ok=True)
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        logger.info(f"Speaker ready. Mic file: {self.mic_file}")

    def generate_speech_wav(self, text: str, path: str = None) -> str | None:
        """Generate a real WAV of `text` using offline Windows SAPI TTS (no API,
        no terms acceptance). Writes 16kHz mono 16-bit PCM — Chrome-friendly for
        the fake microphone. Returns the path, or None on failure."""
        path = path or self.mic_file
        if not text.strip():
            return None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ps_script = (
            "$ErrorActionPreference = 'Stop'; "
            "$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$indianVoice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -match 'en-IN' }; "
            "if ($indianVoice) { $s.SelectVoice($indianVoice[0].VoiceInfo.Name) } "
            "else { "
            "  $engVoice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -match 'en' }; "
            "  if ($engVoice) { $s.SelectVoice($engVoice[0].VoiceInfo.Name) } "
            "} "
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            "try { $s.SetOutputToWaveFile([string]$payload.path, $fmt); "
            "$s.Rate = 0; $s.Speak([string]$payload.text); } finally { $s.Dispose(); }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                input=json.dumps({"text": text.strip(), "path": str(Path(path).resolve())}),
                text=True, encoding="utf-8", errors="replace",
                check=True, capture_output=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if Path(path).exists() and Path(path).stat().st_size > 100:
                logger.info(f"Offline TTS wrote speech WAV: {path}")
                return path
            logger.error("Offline TTS produced empty file.")
            return None
        except Exception as e:
            logger.error(f"Offline TTS (SAPI) failed: {e}")
            return None

    def get_wav_duration(self, path: str) -> float:
        """Return the duration in seconds of a WAV file (0 if unreadable)."""
        try:
            with wave.open(path, "r") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                return frames / float(rate) if rate else 0.0
        except Exception:
            return 0.0

    def _generate_edge_tts(self, text: str, output_path: str) -> str | None:
        try:
            import edge_tts
            import asyncio
            import os
            import subprocess

            mp3_path = str(Path(output_path).with_suffix(".mp3"))

            async def _run_tts():
                communicate = edge_tts.Communicate(text, self.voice)
                await asyncio.wait_for(communicate.save(mp3_path), timeout=60)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(lambda: asyncio.run(_run_tts())).result()
            else:
                asyncio.run(_run_tts())

            if os.path.exists(mp3_path):
                ffmpeg_path = shutil.which("ffmpeg")
                if not ffmpeg_path and Path("ffmpeg.exe").is_file():
                    ffmpeg_path = str(Path("ffmpeg.exe").resolve())
                if not ffmpeg_path:
                    logger.error("ffmpeg was not found on PATH or in the project directory")
                    return None

                subprocess.run(
                    [ffmpeg_path, "-nostdin", "-y", "-i", mp3_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", output_path],
                    check=True, capture_output=True, timeout=60
                )
                os.remove(mp3_path)
                logger.info(f"Edge TTS generated and converted to WAV: {output_path}")
                return output_path
        except Exception as e:
            logger.error(f"Edge TTS failed: {e}")
        return None

    def text_to_speech(self, text: str, filename: str = "speaking_output.wav") -> str | None:
        """Try configured TTS model; fall back to offline SAPI. Returns WAV path."""
        output_path = str(Path(self.temp_dir) / filename)

        if self.model == "edge-tts":
            result = self._generate_edge_tts(text, output_path)
            if result:
                return result
            logger.warning("edge-tts failed, falling back to offline SAPI.")
            return self.generate_speech_wav(text, output_path)

        # If the user wants the best Indian pronunciation, SAPI 'en-IN' is much better
        # than the fallback Arabic model that is currently configured.
        if self.model == "offline" or "arabic" in self.model:
            logger.info("Using offline SAPI TTS for better Indian pronunciation.")
            return self.generate_speech_wav(text, output_path)

        try:
            response = self.client.audio.speech.create(
                model=self.model, voice=self.voice, input=text, response_format="wav"
            )
            response.write_to_file(output_path)
            return output_path
        except Exception as e:
            logger.warning(f"Groq TTS failed ({e}); using offline SAPI.")
            return self.generate_speech_wav(text, output_path)

    def prepare_in_browser(self, page, file_path: str) -> bool:
        import base64
        try:
            mime_type = "audio/wav" if file_path.endswith(".wav") else "audio/mp3"
            with open(file_path, "rb") as f: audio_data = f.read()
            audio_b64 = base64.b64encode(audio_data).decode("utf-8")

            js_code = f"""
            (function() {{
                try {{
                    const mimeType = '{mime_type}';
                    const base64Data = '{audio_b64}';
                    const AudioContext = window.AudioContext || window.webkitAudioContext;
                    const ctx = new AudioContext();

                    const audio = new Audio('data:' + mimeType + ';base64,' + base64Data);
                    audio.crossOrigin = "anonymous";

                    const source = ctx.createMediaElementSource(audio);
                    const dest = ctx.createMediaStreamDestination();
                    source.connect(dest);
                    source.connect(ctx.destination);

                    window.injectedAudioElement = audio;
                    window.injectedAudioStream = dest.stream;
                    window.injectedAudioContext = ctx;
                    console.log("Audio injection prepared successfully!");
                    return true;
                }} catch (e) {{
                    console.error("Failed to prepare audio injection:", e);
                    return false;
                }}
            }})()
            """
            return page.evaluate(js_code)
        except Exception as e:
            logger.error(f"Failed to prepare audio injection: {e}")
            return False

    def play_in_browser(self, page) -> bool:
        try:
            js_code = """
            new Promise((resolve) => {
                try {
                    const audio = window.injectedAudioElement;
                    if (!audio) {
                        console.error("No injected audio element found.");
                        resolve(false);
                        return;
                    }

                    let timer;
                    const finish = success => { clearTimeout(timer); resolve(success); };
                    audio.onended = () => finish(true);
                    audio.onerror = (e) => {
                        console.error("Audio playback error:", e);
                        finish(false);
                    };

                    const ready = window.injectedAudioContext
                        ? window.injectedAudioContext.resume() : Promise.resolve();
                    ready.then(() => audio.play()).then(() => {
                        console.log("Audio playback started.");
                        const timeout = Number.isFinite(audio.duration)
                            ? (audio.duration + 10) * 1000 : 120000;
                        timer = setTimeout(() => { audio.pause(); finish(false); }, timeout);
                    }).catch(e => {
                        console.error("Play error:", e);
                        finish(false);
                    });
                } catch (e) {
                    console.error("Play execution error:", e);
                    resolve(false);
                }
            })
            """
            return page.evaluate(js_code)
        except Exception as e:
            logger.error(f"Failed to play audio injection: {e}")
            return False

    def cleanup_in_browser(self, page) -> bool:
        try:
            js_code = """
            (function() {
                try {
                    if (window.injectedAudioStream) {
                        window.injectedAudioStream.getTracks().forEach(track => track.stop());
                        window.injectedAudioStream = null;
                    }
                    if (window.injectedAudioElement) {
                        window.injectedAudioElement.pause();
                        window.injectedAudioElement = null;
                    }
                    if (window.injectedAudioContext) {
                        window.injectedAudioContext.close();
                        window.injectedAudioContext = null;
                    }
                    console.log("Audio injection cleaned up.");
                    return true;
                } catch (e) {
                    console.error("Cleanup error:", e);
                    return false;
                }
            })()
            """
            return page.evaluate(js_code)
        except Exception as e:
            logger.error(f"Failed to clean up audio injection: {e}")
            return False

    def cleanup(self):
        for f in Path(self.temp_dir).glob("speaking_output*.wav"):
            try: f.unlink()
            except Exception: pass
