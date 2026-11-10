import logging

def handle_writing_task(browser, ai, config, logger):
    logger.info("Handling Writing Task...")
    sel = config["selectors"]["exam"]
    textarea_sel = sel["textarea"]
    check_sel = sel["check_button"]
    continue_sel = sel["continue_button"]
    submit_sel = sel.get("submit_button", "button:has-text('Submit')")

    for attempt in range(3):
        try:
            # Check if the textarea is actually editable (not readonly review screen).
            is_editable = False
            try:
                is_editable = browser.page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        return el && !el.readOnly && !el.disabled;
                    }""", textarea_sel
                )
            except Exception:
                pass

            if not is_editable:
                # Textarea is readonly (review screen) or missing. Try to advance.
                logger.info("  Textarea is readonly/missing. Trying to advance...")
                if browser.element_exists(submit_sel, timeout=1000):
                    browser.safe_click(submit_sel, timeout=3000)
                    return "TEST_DONE"
                if browser.element_exists(continue_sel, timeout=2000):
                    browser.safe_click(continue_sel, timeout=3000)
                    return True
                if not browser.element_exists(textarea_sel, timeout=2000):
                    return "TEST_DONE"
                # Truly stuck — wait and retry.
                browser.page.wait_for_timeout(2000)
                continue

            prompt = browser.get_writing_prompt()
            if not prompt:
                logger.warning(f"  Attempt {attempt+1}: Prompt not found.")
                continue
            logger.info(f"  [WRITING] Prompt: {prompt[:200]}...")

            essay = ai.answer_writing(prompt)
            if not essay:
                logger.warning(f"  Attempt {attempt+1}: AI failed to generate essay.")
                continue
            logger.info(f"  [WRITING] Generated Essay: {essay[:200]}...")

            browser.fill_text(textarea_sel, essay)
            browser.click(check_sel)
            browser.page.wait_for_timeout(500)

            # Check for Submit (end of test).
            if browser.element_exists(submit_sel, timeout=1000):
                browser.safe_click(submit_sel, timeout=4000)
                logger.info("Writing test submitted.")
                return "TEST_DONE"

            if browser.element_exists(continue_sel, timeout=10000):
                browser.safe_click(continue_sel, timeout=4000)
                logger.info("Writing task completed.")
                return True

        except Exception as e:
            logger.error(f"  Attempt {attempt+1} failed: {e}")
            browser.page.wait_for_timeout(2000)

    return False
