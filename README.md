# Bharat English Corporate Automation Bot

This bot helps with Bharat English Corporate tasks.

Based on [shreyash1234566/bet-bot](https://github.com/shreyash1234566/bet-bot), with
updated Speaking automation and regression tests.

## What It Does
- Logs in to the site
- Opens Writing, Reading, Listening, and Speaking tasks
- Uses Groq to help answer questions
- Submits work and moves to the next step

## Quick Start
1. Clone the project.
   ```bash
   git clone https://github.com/legend048/bet-bot.git
   cd bet-bot
   ```
2. Install `ffmpeg` on your system.
3. Install Python packages and Chromium.
   ```bash
   python -m pip install -r requirements.txt
   python -m playwright install --with-deps chromium
   ```
4. Create your `.env` file.
   ```bash
   cp .env.example .env
   ```
5. Open `config.yaml` and check the settings.

## Groq API Key
You need a Groq API key for the bot to work.

1. Open: https://console.groq.com/keys
2. Sign in to your Groq account.
3. Click to create a new API key.
4. Copy the key.

Put this in your `.env` file:

```bash
GROQ_API_KEY=your_real_groq_api_key_here
BET_USERNAME=your_real_bharat_english_username_or_email
BET_PASSWORD=your_real_bharat_english_password
```

## Run
```bash
python main.py --skill speaking
```

The default configuration runs Speaking only, in the first two assigned topics.
It skips completed lessons and seeks videos to their final second so the site
unlocks Practice, then opens Learn and completes the Speaking questions.
Set `run.skip_videos: false` to watch videos in full. Locked topics that require
`Unlock Now` are left alone. The bot verifies that a submitted lesson shows Done
before moving on; a missing control or locked prerequisite is reported as a failure.
Final submission handles the “Ready to submit?” confirmation dialog, including
when the site opens it automatically after the last question.

To verify one new lesson before a longer run:

```bash
python main.py --skill speaking --max-lessons 1
```

Use `--skill all` to run all four sections, or choose `writing`, `reading`, or
`listening`. Configure `run.max_units`, `run.video_timeout`, and `run.max_lessons`
in `config.yaml`; `max_lessons: 0` runs all remaining lessons in those topics.

On Windows with this project's virtual environment:

```powershell
.\env\Scripts\python.exe main.py --skill speaking
```

The updated login selects Email when the site opens on Mobile OTP. Course
navigation uses Adaptive Learning. Speaking currently uses offline Windows SAPI
because the Edge TTS service returned HTTP 403 during verification. To retry Edge
later, set `ai.tts_model: edge-tts`; audio conversion supports ffmpeg on PATH or in
the project directory, and SAPI remains the fallback.
Text generation uses the account's available `openai/gpt-oss-120b` model on Groq;
the previously configured Llama model returned `model_not_found`.

Run the isolated browser regression checks with:

```bash
python -m unittest discover -s tests -v
```

## Important

- `ffmpeg` must be available in your system PATH.
- If Chromium does not start, run `python -m playwright install --with-deps chromium` again.
- In GitHub Codespaces, set `browser.headless: true` in `config.yaml` or run the bot with `xvfb-run -a python main.py`.
- Do not commit your real `.env` file.
