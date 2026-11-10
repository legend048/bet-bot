import re
import time


RECORD_SELECTORS = [
    "button[aria-label='Start recording' i]",
    "button[aria-label='Record' i]",
    "button[hint='Record']",
    "button:text-is('Start Recording')",
    ".mic-button",
]
STOP_SELECTORS = [
    "button[aria-label='Stop recording' i]",
    "button[aria-label='Stop' i]",
    "button:text-is('Stop Recording')",
    "button:text-is('Stop')",
]
INSTRUCTION_KEYWORDS = [
    "read aloud", "read the sentence", "click on the mic", "start recording",
    "record again", "tap check", "check below", "extempore",
    "express your view", "happy with it", "explanation", "speak the sentence",
    "for the below", "& record", "speak about these points", "need help",
]


def _find_button(browser, selectors, timeout=4000):
    """Return one visible, enabled locator rather than a strict-mode CSS union."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            for button in browser.page.locator(selector).all():
                if button.is_visible() and button.is_enabled():
                    return button
        browser.page.wait_for_timeout(200)
    return None


def _is_instruction(text):
    low = text.strip().lower()
    return (any(word in low for word in INSTRUCTION_KEYWORDS)
            or bool(re.match(r"^max\s+\d+\s+seconds", low))
            or low in {"skip", "continue", "skip continue", "check", "submit"})


def handle_speaking_task(browser, ai, speaker, config, logger):
    """Record generated speech and advance only after the site enables it."""
    logger.info("Handling Speaking Task...")
    selectors = config["selectors"]["exam"]
    for attempt in range(2):
        recording = False
        advanced = False
        try:
            content = browser.page.locator("main")
            progress = re.search(r"\b\d+/\d+\b", content.inner_text()) if content.count() else None
            blocks = list(dict.fromkeys(browser.get_speaking_info()))
            joined = " ".join(blocks).lower()
            read_aloud = "read aloud" in joined or "read the sentence" in joined
            prose = [block for block in blocks if not _is_instruction(block) and len(block.split()) >= 3]
            if not prose:
                raise RuntimeError("No speaking prompt found on the page.")
            prompt = max(prose, key=len)
            text = prompt if read_aloud else ai.generate_speech_text(prompt)
            if not text or not text.strip():
                raise RuntimeError("No speech text was generated.")
            logger.info("%s: %s", "Read aloud" if read_aloud else "Extempore", text[:100])
            audio = speaker.text_to_speech(text)
            if not audio:
                raise RuntimeError("Speech generation failed.")
            limit = re.search(r"max\s+(\d+)\s+seconds", joined)
            if limit and speaker.get_wav_duration(audio) >= int(limit[1]) - 1:
                raise RuntimeError("Generated speech is longer than the recording limit.")
            record = _find_button(browser, RECORD_SELECTORS)
            if record is None:
                raise RuntimeError("No Record button found.")
            if not speaker.prepare_in_browser(browser.page, audio):
                raise RuntimeError("Could not prepare the microphone audio stream.")
            record.click()
            recording = True
            browser.page.wait_for_timeout(3200)
            if not speaker.play_in_browser(browser.page):
                raise RuntimeError("Speech playback failed or timed out.")
            stop = _find_button(browser, STOP_SELECTORS)
            if stop is None:
                raise RuntimeError("No Stop recording button found after playback.")
            stop.click()
            recording = False
            actions = [selectors["check_button"], selectors["submit_button"], selectors["continue_button"]]
            action = _find_button(browser, actions, timeout=30000)
            if action is None:
                raise RuntimeError("The site did not enable Check, Continue, or Submit after recording.")
            label = action.inner_text().strip().lower()
            if "submit" in label:
                return "READY_TO_SUBMIT"
            action.click()
            advanced = True
            if "check" in label:
                action = _find_button(browser, actions[1:], timeout=30000)
                if action is None:
                    raise RuntimeError("The recording was checked, but the next step did not become available.")
                label = action.inner_text().strip().lower()
                if "submit" in label:
                    return "READY_TO_SUBMIT"
                action.click()
            logger.info("Speaking recording accepted; clicked %s.", label)
            if progress and "submit" not in label:
                browser.page.wait_for_function(
                    "previous => { const text = document.querySelector('main')?.innerText || ''; "
                    "const progress = text.match(/\\b\\d+\\/\\d+\\b/); return !progress || progress[0] !== previous; }",
                    arg=progress[0], timeout=30000,
                )
            return True
        except Exception as error:
            logger.error("Speaking attempt %s failed: %s", attempt + 1, error)
            if advanced:
                return False
        finally:
            if recording:
                stop = _find_button(browser, STOP_SELECTORS, timeout=1000)
                if stop is not None:
                    stop.click()
            speaker.cleanup_in_browser(browser.page)
            speaker.cleanup()
    return False
