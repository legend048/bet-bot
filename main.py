import os
import sys
import argparse
import logging
import time
from playwright.sync_api import expect
import yaml
from dotenv import load_dotenv
from pathlib import Path

from modules.logger_setup import setup_logger
from modules.browser import BrowserController
from modules.ai_answerer import AIAnswerer
from modules.transcriber import Transcriber
from modules.speaker import Speaker

# Import task handlers
from tasks.writing import handle_writing_task
from tasks.reading import handle_reading_task
from tasks.listening import handle_listening_task
from tasks.speaking import handle_speaking_task
from tasks.adaptive import process_adaptive_skill

load_dotenv()

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f: return yaml.safe_load(f)

class BrowserClosed(Exception):
    """Raised when the browser/page has been closed and the bot cannot continue."""
    pass


def confirmation_submit_button(page):
    return page.get_by_role("dialog").filter(has_text="Ready to submit?").get_by_role(
        "button", name="Submit", exact=True
    )


def final_submit_button(page):
    """Prefer the confirmation dialog over the covered lesson Submit button."""
    confirmation = confirmation_submit_button(page)
    if confirmation.is_visible():
        return confirmation
    scope = page.locator("main") if page.locator("main").count() else page
    return scope.get_by_role("button", name="Submit", exact=True)


def submit_lesson(browser, logger, timeout=30000):
    page = browser.page
    button = final_submit_button(page)
    button.wait_for(state="visible", timeout=timeout)
    try:
        expect(button).to_be_enabled(timeout=timeout)
    except AssertionError as error:
        raise RuntimeError("The visible lesson Submit button is disabled.") from error
    # Let Playwright report overlays, detached elements, and other click
    # failures accurately rather than calling every failure 'disabled'.
    confirmed = confirmation_submit_button(page).is_visible()
    button.click(timeout=timeout)
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        confirmation = confirmation_submit_button(page)
        if confirmation.is_visible() and not confirmed:
            expect(confirmation).to_be_enabled(timeout=timeout)
            confirmation.click(timeout=timeout)
            confirmed = True
        # Opening the dialog hides the main area from accessibility, but does
        # not mean the lesson was submitted. Wait for the actual view to leave.
        scope = page.locator("main") if page.locator("main").count() else page
        pending = scope.get_by_role("button", name="Submit", exact=True, include_hidden=True)
        if not any(candidate.is_visible() for candidate in pending.all()) and not confirmation.is_visible():
            logger.info("Final lesson submission accepted.")
            return
        page.wait_for_timeout(200)
    raise RuntimeError("Submit was clicked, but the lesson completion page did not appear.")

def detect_task_type(browser, indicator_timeout=6000):
    # The "Check" button is the universal signal that we're on a question task
    # (Reading/Listening/Writing all use it). Reading pages render their options
    # with markup we don't always recognize and have no audio, so without this
    # the reading task was never detected.
    if confirmation_submit_button(browser.page).is_visible():
        return "submit"
    # Writing: a text area to type into.
    if browser.element_exists("textarea", timeout=500):
        return "writing"

    # Speaking: a microphone / record button.
    speaking_selectors = [
        "button[aria-label='Start recording']",
        "button[aria-label='Record']",
        "button[hint='Record']",
        "button[aria-label='Start Recording']",
        "button:has-text('Start Recording')",
        ".mic-button",
        "button.btn-circle",
    ]
    for ss in speaking_selectors:
        if browser.element_exists(ss, timeout=300):
            return "speaking"
    try:
        if browser.page.locator("text=Start Recording").count() > 0:
            return "speaking"
    except Exception:
        pass

    if final_submit_button(browser.page).is_visible():
        return "submit"

    # A speaking recording preview can also contain <audio>.
    if (browser.element_exists("button[aria-label='Play audio']", timeout=500)
            or browser.element_exists("button[hint='Play audio']", timeout=500)
            or browser.element_exists("audio", timeout=500)):
        return "listening"

    # Reading / generic MCQ: recognizable option elements OR just a Check button.
    for s in ["div.cursor-pointer.shadow-md", ".option", ".choice", "div[role='button'].answer", "li.answer"]:
        if browser.element_exists(s, timeout=500):
            return "reading"
    if browser.element_exists("button:has-text('Check')", timeout=indicator_timeout):
        return "reading"

    return "unknown"

def detect_task_with_retry(browser, is_first_task, logger):
    """Detect the task type. On the FIRST task of a pass the page may still be
    loading (Reading/Speaking load slowly), so we retry several times before
    concluding there's no task. On later tasks a single 'unknown' means the
    test is finished, so we don't waste time retrying."""
    task_type = detect_task_type(browser)
    if task_type != "unknown" or not is_first_task:
        return task_type
    # First task came back unknown — give the page more time to render.
    for attempt in range(6):
        browser.page.wait_for_timeout(2500)
        task_type = detect_task_type(browser)
        logger.info(f"   (waiting for first task to load... attempt {attempt+1}: {task_type})")
        if task_type != "unknown":
            return task_type
    # Still unknown after many retries — dump the DOM so we can see the real
    # markup and refine selectors if needed.
    logger.warning("   First task still 'unknown' after retries — dumping DOM for diagnosis.")
    browser.dump_clickable_elements()
    return "unknown"

def run_two_pass_test(browser, ai, transcriber, speaker, config, logger):
    """Run one lesson test. Listening uses the two-pass network-harvest strategy
    (Pass 1 harvest → refresh → Pass 2 replay). Reading/Writing/Speaking are
    solved directly in a SINGLE pass (Reading via Groq AI), which is much faster."""
    browser.reset_harvest()

    # Peek the first task type to choose the strategy.
    first_type = detect_task_with_retry(browser, is_first_task=True, logger=logger)
    use_two_pass = (first_type == "listening")
    passes = [1, 2] if use_two_pass else [2]  # non-listening = single accuracy pass
    if not use_two_pass:
        logger.info(f"⚡ Single-pass mode (first task: {first_type}).")

    for pass_number in passes:
        if pass_number == 1:
            logger.info("═══ PASS 1: HARVESTING ANSWERS (guessing quickly) ═══")
            browser.is_harvesting = True
        else:
            logger.info("═══ ANSWERING TASKS ═══")
            logger.info(f"   Answers harvested in sequence: {len(browser.answer_sequence)}")
            browser.is_harvesting = False
            browser.reset_replay()

        task_count = 0
        while task_count < 30:
            if not browser.is_alive():
                raise BrowserClosed("Browser/page was closed during the test.")
            task_type = detect_task_with_retry(browser, is_first_task=(task_count == 0), logger=logger)
            logger.info(f"[Pass {pass_number}] Task {task_count+1} detected as: {task_type}")

            success = False
            if task_type == "writing":
                success = handle_writing_task(browser, ai, config, logger)
            elif task_type == "reading":
                success = handle_reading_task(browser, ai, config, logger)
            elif task_type == "listening":
                success = handle_listening_task(browser, ai, transcriber, config, logger)
            elif task_type == "speaking":
                success = handle_speaking_task(browser, ai, speaker, config, logger)
            else:
                # The last Speaking answer first advances to a 5/5 summary.
                # Submit appears there with no mic/Check control, so an unknown
                # task is not proof the lesson has already been submitted.
                if pass_number != 1 and final_submit_button(browser.page).is_visible():
                    submit_lesson(browser, logger)
                    return
                # No recognizable task — test probably finished
                exit_sel = config["selectors"]["exam"].get("exit_button")
                close_sel = config["selectors"]["exam"].get("close_button")

                if pass_number == 1:
                    logger.info(f"Pass 1 finished! Harvested {len(browser.answer_sequence)} answers.")
                    logger.info("Refreshing page to restart test for Pass 2...")
                    browser.reload()
                    browser.page.wait_for_timeout(4000)
                    break
                else:
                    if browser.safe_click(exit_sel, timeout=3000):
                        logger.info("Test finished. Exited practice.")
                    elif browser.safe_click(close_sel, timeout=3000):
                        logger.info("Test finished. Closed practice.")
                    else:
                        logger.info("Test fully completed.")
                    return

            if success == "END_OF_TEST":
                # Pass 1: end of test reached — refresh and start Pass 2
                logger.info(f"Pass 1 finished! Harvested {len(browser.answer_sequence)} answers.")
                logger.info("Refreshing page to restart test for Pass 2...")
                browser.reload()
                browser.page.wait_for_timeout(4000)
                break
            elif success == "READY_TO_SUBMIT":
                submit_lesson(browser, logger)
                return
            elif success == "TEST_DONE":
                # Pass 2: the final Submit was clicked (or no Check button left).
                logger.info("Final submission clicked. Finalizing practice.")
                exit_sel = config["selectors"]["exam"].get("exit_button")
                close_sel = config["selectors"]["exam"].get("close_button")
                browser.safe_click(exit_sel, timeout=2500) or browser.safe_click(close_sel, timeout=2500)
                return
            elif success:
                task_count += 1
                logger.info(f"[Pass {pass_number}] Task {task_count} done.")
                browser._settle()
            else:
                logger.error(f"[Pass {pass_number}] Task failed. Stopping this test.")
                return

def _lesson_key(btn):
    """Build a stable identifier for the lesson a Practice/Retry button belongs
    to, based on its card's title text (digits/percent stripped so the score
    changing doesn't change the key)."""
    try:
        return btn.evaluate(
            """el => {
                let card = el;
                for (let i = 0; i < 10 && card; i++) {
                    if (card.querySelector && card.querySelector('h1,h2,h3,h4')) break;
                    card = card.parentElement;
                }
                let title = '';
                if (card) {
                    const h = card.querySelector('h1,h2,h3,h4');
                    title = h ? h.innerText : (card.innerText || '');
                }
                return title.replace(/[0-9%]/g, '').replace(/\\s+/g, ' ').trim().slice(0, 50);
            }"""
        ) or ""
    except Exception:
        return ""

def process_lessons(browser, ai, transcriber, speaker, config, logger, skill_name):
    """On the current lessons page, keep taking tests until no Practice/Retry
    button remains. After EVERY test we reload the page and re-scan from scratch
    so newly-unlocked lessons get picked up. A lesson that fails to complete
    after 2 attempts is skipped individually (we never abandon the whole unit)."""
    sel = config["selectors"]
    lessons_url = browser.page.url
    start_test_sel = sel["lessons"].get("start_test_button", "button:has-text('Retry'), button:has-text('Practice')")
    if skill_name.lower() == "writing":
        start_test_sel = "button:has-text('Practice')"

    failed = {}          # lesson_key -> failed attempt count
    attempting = None    # key of the lesson we just tried (to verify completion)
    max_total = 80       # generous safety cap
    total = 0

    while total < max_total:
        # Re-scan the page fresh every time.
        browser.go_to(lessons_url)
        browser.page.wait_for_timeout(3000)

        buttons = browser.page.query_selector_all(start_test_sel)
        keyed = [(_lesson_key(b), b) for b in buttons]
        present_keys = {k for k, _ in keyed if k}

        # Verify whether the lesson we just attempted actually got completed.
        if attempting is not None:
            if attempting in present_keys:
                failed[attempting] = failed.get(attempting, 0) + 1
                logger.warning(f"[{skill_name}] Lesson '{attempting[:40]}' not completed (attempt {failed[attempting]}).")
            else:
                logger.info(f"[{skill_name}] Lesson '{attempting[:40]}' completed ✅")
                failed.pop(attempting, None)
            attempting = None

        # Choose the next doable lesson (not yet failed twice).
        candidates = [(k, b) for k, b in keyed if failed.get(k, 0) < 2]
        skipped = len(buttons) - len(candidates)
        logger.info(f"📋 [{skill_name}] Practice/Retry buttons: {len(buttons)} | doable: {len(candidates)} | skipped: {skipped}")

        if not candidates:
            if buttons:
                logger.warning(f"[{skill_name}] {len(buttons)} lesson(s) left but each failed twice — skipping them.")
            else:
                logger.info(f"✅ [{skill_name}] All lessons in this unit are done!")
            break

        key, btn = candidates[0]
        logger.info(f"▶️ [{skill_name}] Starting lesson: '{key[:40]}'")
        try:
            btn.click()
            browser._settle()
            browser.page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"Failed to click lesson button: {e}")
            failed[key] = failed.get(key, 0) + 1
            total += 1
            continue

        attempting = key
        try:
            run_two_pass_test(browser, ai, transcriber, speaker, config, logger)
        except BrowserClosed:
            raise  # genuinely dead browser — let the pipeline stop cleanly
        except Exception as e:
            logger.error(f"[{skill_name}] Lesson '{key[:40]}' crashed: {e}. Recovering and continuing.")
            failed[key] = failed.get(key, 0) + 1
            attempting = None  # already counted this failure
        total += 1
        logger.info(f"[{skill_name}] Finished test #{total}. Re-scanning lessons page...")

def process_skill(browser, ai, transcriber, speaker, config, logger, skill_name, skill_start_sel):
    """Navigate into one FluentEdge skill (Listening/Reading/Speaking/Writing),
       drill down Level → every Unit, and complete every lesson in each unit."""
    sel = config["selectors"]
    logger.info(f"╔══════════════════════════════════════════════╗")
    logger.info(f"║  SKILL: {skill_name.upper()}")
    logger.info(f"╚══════════════════════════════════════════════╝")

    def robust_navigate(selector: str, log_name: str, use_locator_first: bool = False):
        logger.info(f"DEBUG: Attempting to click {log_name}...")
        for attempt in range(3):
            old_url = browser.page.url
            if use_locator_first:
                try:
                    browser.page.locator(selector).first.click()
                except Exception:
                    pass
            else:
                browser.click(selector, wait_for_nav=True)
            browser._settle()
            if browser.page.url != old_url:
                logger.info(f"DEBUG: Navigation successful for {log_name}. New URL: {browser.page.url}")
                return True
            logger.warning(f"DEBUG: Click {attempt+1} on {log_name} didn't navigate. Retrying...")
        return False

    # Always start from the FluentEdge dashboard for each skill
    if browser.element_exists(sel["navigation"]["sidebar_ai_fluentedge"]):
        browser.page.locator(sel["navigation"]["sidebar_ai_fluentedge"]).first.click()
        browser._settle()
        browser.page.wait_for_timeout(1500)

    # Dashboard: click this skill's Start button
    logger.info(f"DEBUG: Clicking {skill_name} Start on dashboard...")
    if browser.element_exists(skill_start_sel, timeout=30000):
        robust_navigate(skill_start_sel, f"Dashboard {skill_name} Start")
    else:
        raise RuntimeError(f"No {skill_name} course entry found on the dashboard.")
    logger.info(f"DEBUG: URL after Dashboard: {browser.page.url}")

    if "/adaptive-learning/skill/" in browser.page.url:
        return process_adaptive_skill(
            browser, config, logger,
            lambda: run_two_pass_test(browser, ai, transcriber, speaker, config, logger),
        )

    # Level selection
    if browser.element_exists(sel["levels"]["start_button"]):
        robust_navigate(sel["levels"]["start_button"], "Level Start button")
    elif browser.element_exists(sel["levels"]["revisit_button"]):
        robust_navigate(sel["levels"]["revisit_button"], "Level Revisit button", use_locator_first=True)
    else:
        logger.info("DEBUG: No Level button found!")
    logger.info(f"DEBUG: URL after Level: {browser.page.url}")

    # ─── UNIT LOOP: enter EVERY accessible unit on "What You'll Learn" ──
    # A unit can show "Start Learning" (new), "Review" (has progress/done), or
    # "Continue Learning"/"Resume" (partial). We can't tell from the button text
    # alone whether a unit still has unfinished lessons — a partially-done unit
    # shows "Review" too. So we enter EVERY accessible (unlocked) unit by index
    # and run the lesson loop inside; fully-completed units finish instantly.
    # We iterate by a visited counter so no unit is ever skipped. (Locked units
    # showing "Unlock" are not entered — that may spend credits.)
    # ──────────────────────────────────────────────────────────────────
    units_url = browser.page.url
    accessible_unit_sel = sel["units"].get(
        "accessible_unit",
        "button:has-text('Start Learning'), button:has-text('Continue Learning'), button:has-text('Resume'), button:has-text('Review')"
    )

    visited = 0
    max_units = config.get("run", {}).get("max_units", 2)

    while visited < max_units:
        browser.go_to(units_url)
        browser.page.wait_for_timeout(2500)

        unit_buttons = browser.page.query_selector_all(accessible_unit_sel)
        total = len(unit_buttons)
        logger.info(f"🗂️ [{skill_name}] Accessible units: {total} | Already visited: {visited}")

        if visited >= total:
            logger.info(f"✅ [{skill_name}] All {total} accessible units processed! Moving to next skill.")
            break

        logger.info(f"📂 [{skill_name}] Entering unit #{visited} (of {total})")
        try:
            unit_buttons[visited].click()
            browser._settle()
            browser.page.wait_for_timeout(2500)
        except Exception as e:
            logger.error(f"Failed to enter unit #{visited}: {e}")
            visited += 1
            continue

        # Complete every lesson inside this unit
        process_lessons(browser, ai, transcriber, speaker, config, logger, skill_name)
        visited += 1
        logger.info(f"[{skill_name}] Finished unit #{visited-1}. Returning to unit list...")

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the selected Bharat English course.")
    parser.add_argument("--skill", choices=["speaking", "writing", "reading", "listening", "all"])
    parser.add_argument("--max-lessons", type=int, help="Stop after this many new lessons (0 means all).")
    args = parser.parse_args(argv)
    if args.max_lessons is not None and args.max_lessons < 0:
        parser.error("--max-lessons must be zero or greater")
    return args


def main(argv=None):
    args = parse_args(argv)
    logger = setup_logger()
    logger.info("Starting Bharat English Bot...")

    config = load_config("config.yaml")
    run_config = config.setdefault("run", {})
    if args.max_lessons is not None:
        run_config["max_lessons"] = args.max_lessons
    selected_skills = run_config.get("skills", ["speaking"])
    if args.skill:
        selected_skills = [args.skill] if args.skill != "all" else ["speaking", "writing", "reading", "listening"]

    # Secure Credentials
    config["credentials"] = {
        "username": os.getenv("BET_USERNAME"),
        "password": os.getenv("BET_PASSWORD")
    }

    browser = BrowserController(config)
    failures = []
    try:
        ai = AIAnswerer(config)
        transcriber = Transcriber(config)
        speaker = Speaker(config)
        browser.start()
        if not browser.login():
            logger.error("Login failed. Exiting.")
            return 1

        sel = config["selectors"]
        fe = sel["fluentedge"]

        # ─── PIPELINE: process skills in this exact order ───────────────
        skill_pipeline = [(name.title(), fe[f"{name}_start"]) for name in selected_skills]

        for skill_name, skill_start_sel in skill_pipeline:
            if not browser.is_alive():
                logger.error("Browser is no longer alive. Stopping pipeline.")
                failures.append(skill_name)
                break
            try:
                process_skill(browser, ai, transcriber, speaker, config, logger, skill_name, skill_start_sel)
            except BrowserClosed as e:
                failures.append(skill_name)
                logger.error(f"Browser closed during {skill_name}: {e}. Stopping pipeline.")
                break
            except Exception as e:
                failures.append(skill_name)
                logger.error(f"Error while processing {skill_name}: {e}", exc_info=True)
                if not browser.is_alive():
                    logger.error("Browser died. Stopping pipeline.")
                    break
                logger.info(f"Continuing to next skill despite error in {skill_name}.")

        if failures:
            logger.error("Run stopped with unfinished work in: %s", ", ".join(failures))
        else:
            logger.info("Finished the requested run for: %s", ", ".join(selected_skills))

    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        return 1
    finally:
        browser.close()
        logger.info("Bot execution finished.")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
