"""Navigation and prerequisites for the Adaptive Learning course UI."""

import re
import time

from playwright.sync_api import expect


def lesson_card(page, title):
    return page.locator("section.card").filter(
        has=page.get_by_role("heading", name=title, exact=True)
    )


def dismiss_tour(page):
    skip = page.get_by_role("button", name="Skip for now", exact=True)
    if skip.is_visible():
        skip.click()


def complete_video(page, card, config, logger):
    skip_video = config.get("run", {}).get("skip_videos", False)
    button = card.locator("button[aria-label^='Video,']")
    if not button.count() or not (button.get_attribute("aria-label") or "").endswith(", to do"):
        return
    button.click()
    frame = page.frame_locator("iframe[title='Video Lesson']")
    play = frame.get_by_role("button", name="Play", exact=True)
    play.wait_for(state="visible")
    play.click()
    timeout = config.get("run", {}).get("video_timeout", 900)
    if skip_video:
        timeout = min(timeout, 60)
        logger.info("Skipping to the end of the lesson video.")
    deadline = time.monotonic() + timeout
    last_report = 0
    seeked = False
    while time.monotonic() < deadline:
        status = frame.locator("video").evaluate_all(
            "els => els.map(v => ({ended:v.ended, time:v.currentTime, "
            "duration:Number.isFinite(v.duration) ? v.duration : 0, error:!!v.error}))"
        )
        if status and status[0]["error"]:
            raise RuntimeError("The lesson video could not load.")
        if status and status[0]["ended"]:
            break
        if skip_video and not seeked and status and status[0]["duration"] > 0:
            # Use normal media seeking, then let playback end naturally so the
            # site's player can unlock Practice through its usual ended event.
            frame.locator("video").evaluate("video => { video.currentTime = Math.max(0, video.duration - 1); }")
            seeked = True
        if time.monotonic() - last_report >= 30:
            progress = status[0] if status else {"time": 0, "duration": 0}
            logger.info("Watching prerequisite video: %.0f / %.0f seconds", progress["time"], progress["duration"])
            last_report = time.monotonic()
        page.wait_for_timeout(1000)
    else:
        raise RuntimeError(f"The lesson video did not finish within {timeout} seconds.")
    # Give the embedded player's ended event time to persist before closing it.
    page.wait_for_timeout(1500)
    page.get_by_role("button", name="Close video dialog", exact=True).click()
    # Dialog closure clears aria-hidden asynchronously. Reacquire the card
    # before checking its Learn button rather than mistaking it for absent.
    card.wait_for(state="visible")
    # Some course cards keep the old badge until their next reload. Completion
    # here is based on the player's ended event, not a fabricated progress flag.
    logger.info("Prerequisite video finished.")


def complete_learn(page, card, config, logger):
    button = card.locator("button[aria-label^='Learn,']")
    button.wait_for(state="visible")
    if not (button.get_attribute("aria-label") or "").endswith(", to do"):
        return False
    button.click()
    page.get_by_text("Reading Material", exact=True).wait_for(state="visible")
    logger.info("Reading the lesson's Learn notes.")
    notes = page.locator("main").inner_text()
    if not notes.strip():
        raise RuntimeError("The Learn material is empty.")
    page.wait_for_timeout(config.get("run", {}).get("learn_seconds", 10) * 1000)
    page.locator("main").evaluate("el => el.scrollTo(0, el.scrollHeight)")
    finish = page.get_by_role("button", name="I've finished reading", exact=True)
    expect(finish).to_be_enabled(timeout=15000)
    finish.click()
    page.get_by_role("button", name="Go to practice", exact=True).click()
    return True


def process_adaptive_skill(browser, config, logger, run_lesson):
    """Process assigned topic cards only; never purchase an Unlock Now item."""
    page = browser.page
    page.get_by_text("Course Topics", exact=True).wait_for(state="visible")
    page.locator("[role='button'].card").first.wait_for(state="visible")
    topics_url = page.url
    topics = page.locator("[role='button'].card").evaluate_all(
        "els => els.filter(e => !/\\bUnlock\\b/.test(e.innerText))"
        ".map(e => ({title:e.querySelector('h3')?.innerText.trim(), text:e.innerText}))"
    )
    topics = [topic for topic in topics if topic["title"]]
    max_units = config.get("run", {}).get("max_units", 2)
    limit = config.get("run", {}).get("max_lessons", 0)
    completed = 0
    for topic in topics[:max_units]:
        ratio = re.search(r"(\d+)\s+of\s+(\d+)\s+lessons", topic["text"])
        if ratio and ratio[1] == ratio[2]:
            logger.info("Skipping completed topic: %s", topic["title"])
            continue
        browser.go_to(topics_url)
        page.get_by_role("heading", name=topic["title"], exact=True).click()
        page.locator("section.card h3").first.wait_for(state="visible")
        lessons_url = page.url
        while not limit or completed < limit:
            dismiss_tour(page)
            cards = page.locator("section.card").evaluate_all(
                "els => els.map(e => ({title:e.querySelector('h3')?.innerText.trim(), "
                "done:Array.from(e.querySelectorAll('button')).some(b=>b.disabled && b.innerText.trim()==='Done'), "
                "available:!!e.querySelector(\"button[aria-label^='Video,'],button[aria-label^='Learn,']\")}))"
            )
            remaining = [card for card in cards if card["title"] and not card["done"]]
            candidates = [card for card in remaining if card["available"]]
            if not candidates:
                if remaining:
                    raise RuntimeError(f"{len(remaining)} lessons remain locked in {topic['title']}.")
                logger.info("All lessons complete in %s", topic["title"])
                break
            title = candidates[0]["title"]
            logger.info("Starting lesson: %s", title)
            card = lesson_card(page, title)
            complete_video(page, card, config, logger)
            practice_open = complete_learn(page, card, config, logger)
            if not practice_open:
                practice = card.get_by_role("button", name=re.compile(r"^(?:Start )?Practice(?:,|$)", re.I))
                expect(practice).to_be_enabled(timeout=15000)
                practice.click()
            run_lesson()
            browser.go_to(lessons_url)
            card = lesson_card(page, title)
            expect(card.get_by_role("button", name="Done", exact=True)).to_be_visible(timeout=30000)
            completed += 1
            logger.info("Verified lesson completion: %s (%s this run)", title, completed)
        if limit and completed >= limit:
            logger.info("Reached the requested limit of %s lesson(s).", limit)
            break
    return completed
