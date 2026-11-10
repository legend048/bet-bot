import logging

def _click_first_option(browser, option_selectors):
    # Try the configured option selectors first.
    for opt_sel in option_selectors:
        elements = browser.page.query_selector_all(opt_sel)
        if elements:
            elements[0].click()
            return True
    # Fallback: generic option clicker for pages with unknown markup.
    if browser.click_first_generic_option():
        return True
    return False

def handle_listening_task(browser, ai, transcriber, config, logger):
    logger.info("Handling Listening Task...")
    sel = config["selectors"]["exam"]
    option_selectors = sel.get("option_selectors", [".option", "div[role='button']", ".choice"])
    check_sel = sel["check_button"]

    if browser.is_harvesting:
        # PASS 1: If the answer for this question is already known, click it (it
        # selects a real valid option). Otherwise click any option to advance &
        # trigger answer capture on submit.
        logger.info("  [Harvest] Selecting an option...")
        pre_known = browser.next_harvest_answer()
        selected = False
        if pre_known:
            logger.info(f"  [Harvest] Using pre-known answer: {pre_known[:40]}")
            selected = browser.select_answer_by_text(pre_known, check_sel)
        if not selected:
            _click_first_option(browser, option_selectors)
        if not browser.is_check_enabled(check_sel, timeout=1200):
            browser.select_option_until_check_enabled(check_sel)
    else:
        # PASS 2: Click the CORRECT answer and verify it registered. Never click
        # a wrong option here.
        correct_answer = browser.get_next_answer()
        if not correct_answer:
            correct_answer = browser.get_known_answer(browser.get_reading_content().get("question", ""))
        logger.info(f"  [Accuracy] Replay#{browser.replay_index-1} | Answer: {correct_answer}")
        if correct_answer:
            if not browser.select_answer_by_text(correct_answer, check_sel):
                browser.find_and_click_option(correct_answer)
        else:
            logger.warning("  [Accuracy] No cached answer available for this question!")

    # If there's no Check button at all, the test is over.
    if not browser.element_exists(check_sel, timeout=5000):
        logger.info("  No Check button — test appears finished.")
        return "END_OF_TEST" if browser.is_harvesting else "TEST_DONE"

    # Ensure Check is enabled before clicking (only nudge with a random option
    # during the HARVEST pass — never in accuracy, to avoid wrong answers).
    if not browser.is_check_enabled(check_sel, timeout=1500):
        if browser.is_harvesting:
            if not browser.select_option_until_check_enabled(check_sel):
                logger.warning("  Could not enable Check. Skipping question.")
                return False
        else:
            logger.warning("  [Accuracy] Correct answer not selected; Check still disabled.")
            return False

    # Click Check — waits for enabled, never hangs 30s on a disabled button.
    if not browser.click_check(check_sel, timeout=8000):
        logger.warning("  Check never became clickable. Skipping question.")
        return False

    browser.page.wait_for_timeout(700 if browser.is_harvesting else 250)

    # End-of-test detection via Submit button.
    submit_sel = sel.get("submit_button", "button:has-text('Submit')")
    if browser.element_exists(submit_sel, timeout=800):
        if browser.is_harvesting:
            logger.info("  [Harvest] Submit detected — refreshing instead.")
            return "END_OF_TEST"
        else:
            logger.info("  [Accuracy] Clicking Submit to finalize.")
            browser.safe_click(submit_sel, timeout=4000)
            return "TEST_DONE"

    if browser.element_exists(sel["continue_button"], timeout=5000):
        browser.safe_click(sel["continue_button"], timeout=4000)
        logger.info("Listening task completed.")
        return True

    return False
