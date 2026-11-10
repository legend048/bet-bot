import logging

def handle_reading_task(browser, ai, config, logger):
    """Reading is solved directly with Groq AI (single pass, no network harvest):
    read the passage/question/options off the page, ask the model for the correct
    option, click it, then Check/Continue."""
    logger.info("Handling Reading Task (AI)...")
    sel = config["selectors"]["exam"]
    check_sel = sel["check_button"]

    # Extract the question, passage and options from the page.
    mcq = browser.extract_reading_mcq()
    question = mcq.get("question", "")
    passage = mcq.get("passage", "")
    options = mcq.get("options", [])

    if not options:
        # No options visible yet — maybe the test is finished.
        if not browser.element_exists(check_sel, timeout=3000):
            logger.info("  No options and no Check button — test appears finished.")
            return "TEST_DONE"
        logger.warning("  No options extracted but Check exists. Dumping DOM.")
        browser.dump_clickable_elements()
        return False

    # Ask Groq for the correct option.
    answer = ai.answer_reading_mcq(question, options, passage)
    logger.info(f"  [AI] Q: '{question[:50]}' | {len(options)} options → Answer: '{answer[:50]}'")

    # Click the chosen answer (verified to enable Check).
    if not browser.select_answer_by_text(answer, check_sel):
        browser.find_and_click_option(answer)

    # No Check button at all → test finished.
    if not browser.element_exists(check_sel, timeout=4000):
        logger.info("  No Check button — test appears finished.")
        return "TEST_DONE"

    # Make sure something is selected so Check is enabled.
    if not browser.is_check_enabled(check_sel, timeout=1500):
        browser.select_option_until_check_enabled(check_sel)

    if not browser.click_check(check_sel, timeout=8000):
        logger.warning("  Check never became clickable. Skipping question.")
        return False

    browser.page.wait_for_timeout(300)

    # End-of-test detection via Submit.
    submit_sel = sel.get("submit_button", "button:has-text('Submit')")
    if browser.element_exists(submit_sel, timeout=800):
        logger.info("  Submit detected — finalizing reading test.")
        browser.safe_click(submit_sel, timeout=4000)
        return "TEST_DONE"

    if browser.element_exists(sel["continue_button"], timeout=5000):
        browser.safe_click(sel["continue_button"], timeout=4000)
        logger.info("Reading task completed.")
        return True

    return False
