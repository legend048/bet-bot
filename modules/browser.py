import time
import logging
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, Browser, Playwright

logger = logging.getLogger("BharatBot")

class BrowserController:
    def __init__(self, config: dict):
        self.config = config
        self.browser_cfg = config.get("browser", {})
        self.creds = config.get("credentials", {})
        self.selectors = config.get("selectors", {})
        self._playwright: Playwright = None
        self._browser: Browser = None
        self.page: Page = None
        self._intercepted_audio_url: str = None
        self._intercepted_correct_answer: str = None
        self.known_answers = {}            # question_text -> answer (for fuzzy lookup)
        self.answer_sequence = []          # ordered list of answers, in capture order
        self._captured_questions = set()   # dedupe set for sequence building
        self.replay_index = 0              # pointer used during Pass 2 ordered replay
        self.harvest_pos = 0               # pointer used during Pass 1 harvest selection
        self.is_harvesting = True  # First pass harvests answers, second pass uses them

    def _record_answer(self, q_text: str, answer: str):
        """Store a captured answer in both the dict and the ordered sequence."""
        if not q_text or not answer:
            return
        # Filter out obvious junk captures
        if len(q_text.strip()) < 6:
            return
        if answer.strip().lower() in ("correct answer:", "answer:", "correct answer", "answer"):
            return
        # Store in dict for fuzzy lookup
        self.known_answers[q_text] = answer
        # Build ordered sequence only during the harvesting pass, deduped by question
        if self.is_harvesting and q_text not in self._captured_questions:
            self._captured_questions.add(q_text)
            self.answer_sequence.append(answer)
            logger.info(f"📝 Sequence[{len(self.answer_sequence)-1}] = {answer[:40]}")

    def get_speaking_info(self) -> list:
        """Return the visible text blocks in the content area of a speaking task
        (used to find the read-aloud sentence or the extempore prompt)."""
        try:
            blocks = self.page.evaluate(
                """() => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const out = [];
                    for (const el of document.querySelectorAll('p,h1,h2,h3,h4,h5,span,div')) {
                        const r = el.getBoundingClientRect();
                        if (r.left < 200 || r.top < 110) continue;
                        // Prefer leaf nodes (no child element with the same text).
                        const txt = norm(el.innerText);
                        if (txt.length < 4 || txt.length > 500) continue;
                        out.push(txt);
                    }
                    return out;
                }"""
            )
            return blocks or []
        except Exception as e:
            logger.debug(f"get_speaking_info failed: {e}")
            return []

    def reset_replay(self):
        """Reset the ordered replay pointer before Pass 2."""
        self.replay_index = 0

    def extract_reading_mcq(self) -> dict:
        """Extract the passage, question, and clickable option texts for a
        reading MCQ, restricted to the main content area. Returns
        {question, passage, options:[...]}."""
        try:
            return self.page.evaluate(
                """() => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const navWords = ['check','submit','continue','next','exit','close','learn',
                        'practice','play','record','skip','unlock','review','start','video','done'];
                    const W = window.innerWidth;
                    // Options: clickable cards in the content area.
                    const options = [];
                    const seen = new Set();
                    const els = Array.from(document.querySelectorAll('div,li,label,[role=button],button'));
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.left < 200 || r.top < 120) continue;
                        if (r.width < 40 || r.height < 18) continue;
                        if (r.width > W * 0.92) continue;
                        const txt = norm(el.innerText);
                        if (!txt || txt.length < 1 || txt.length > 200) continue;
                        const low = txt.toLowerCase();
                        if (navWords.some(w => low === w || low.startsWith(w + ' ') || low === w + '.')) continue;
                        const st = window.getComputedStyle(el);
                        if (st.cursor === 'pointer' || el.getAttribute('role') === 'button') {
                            // Keep leaf-ish cards: avoid containers that wrap other options.
                            if (!seen.has(txt)) { seen.add(txt); options.push(txt); }
                        }
                    }
                    // Question: a prominent medium-weight paragraph/heading.
                    let question = '';
                    const q = document.querySelector("p.text-base.font-medium, .question, .question-text, h4, h3");
                    if (q) question = norm(q.innerText);
                    // Passage: the largest text block in the content area.
                    let passage = '', maxLen = 0;
                    for (const el of document.querySelectorAll('p,div,article,section')) {
                        const r = el.getBoundingClientRect();
                        if (r.left < 200 || r.top < 120) continue;
                        const txt = norm(el.innerText);
                        if (txt.length > maxLen && txt.length < 4000) { maxLen = txt.length; passage = txt; }
                    }
                    return { question, passage, options };
                }"""
            )
        except Exception as e:
            logger.debug(f"extract_reading_mcq failed: {e}")
            return {"question": "", "passage": "", "options": []}

    def reset_harvest(self):
        """Clear all harvested answers before starting a brand-new lesson/test."""
        self.known_answers = {}
        self.answer_sequence = []
        self._captured_questions = set()
        self.replay_index = 0
        self.harvest_pos = 0
        self.is_harvesting = True
        logger.info("🔄 Harvest state reset for new lesson.")

    def next_harvest_answer(self) -> str:
        """During the harvest pass, return the already-known answer for the
        current question position if one exists (Reading delivers all answers on
        page load), else None. Advances the harvest position each call."""
        ans = None
        if 0 <= self.harvest_pos < len(self.answer_sequence):
            ans = self.answer_sequence[self.harvest_pos]
        self.harvest_pos += 1
        return ans

    def get_next_answer(self) -> str:
        """Return the next answer in capture order (used for Pass 2 ordered replay)."""
        if 0 <= self.replay_index < len(self.answer_sequence):
            ans = self.answer_sequence[self.replay_index]
            self.replay_index += 1
            return ans
        return None

    def start(self):
        logger.info("Starting browser...")
        self._playwright = sync_playwright().start()
        launch_args = ["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]

        # Virtual microphone: feed a WAV file as the mic input (so the platform
        # records real speech, not the fake-device beep). Ensure the file exists
        # at launch (create a short silence placeholder if needed), then point
        # Chrome at it. We overwrite this file per speaking question.
        mic_file = self.browser_cfg.get("virtual_mic_file")
        if mic_file:
            mic_path = Path(mic_file)
            mic_path.parent.mkdir(parents=True, exist_ok=True)
            if not mic_path.exists():
                try:
                    import wave
                    with wave.open(str(mic_path), "w") as w:
                        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                        w.writeframes(b"\x00\x00" * 16000)  # 1s silence
                except Exception as e:
                    logger.warning(f"Could not create placeholder mic file: {e}")
            if mic_path.exists():
                launch_args.append(f"--use-file-for-fake-audio-capture={mic_path.resolve()}")
                launch_args.append("--autoplay-policy=no-user-gesture-required")
                logger.info(f"Using virtual microphone file: {mic_path.resolve()}")

        self._browser = self._playwright.chromium.launch(
            headless=self.browser_cfg.get("headless", False),
            slow_mo=self.browser_cfg.get("slow_mo", 50),
            args=launch_args
        )
        context = self._browser.new_context(accept_downloads=True)
        self.page = context.new_page()
        self.page.add_init_script("""
            window.injectedAudioStream = null;
            if (navigator.mediaDevices) {
                const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async function(constraints) {
                    if (constraints && constraints.audio && window.injectedAudioStream) {
                        console.log("Injected custom audio stream into getUserMedia!");
                        return window.injectedAudioStream;
                    }
                    return originalGetUserMedia(constraints);
                };
            }
        """)
        self.page.set_default_timeout(self.browser_cfg.get("timeout", 30000))

        self.known_answers = {}

        def global_json_logger(response):
            try:
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type and response.status == 200:
                    data = response.json()

                    def scan_for_answers(obj, parent_question=None):
                        if isinstance(obj, dict):
                            # Try to find question text in this object or parent context
                            q_text = obj.get("question") or obj.get("questionText") or obj.get("question_text") or parent_question

                            # Strategy 1: Standard format {question, amcq/mcq/msq: {answer, option1...}}
                            for q_type in ["amcq", "mcq", "msq"]:
                                if q_type in obj and q_text:
                                    q_data = obj[q_type]
                                    if isinstance(q_data, dict) and "answer" in q_data:
                                        ans_idx = q_data["answer"]
                                        opt_key = f"option{ans_idx}"
                                        if opt_key in q_data:
                                            self._record_answer(q_text, q_data[opt_key])

                            # Strategy 2: {question, correctAnswer/correct_answer/answer}
                            if q_text:
                                for ans_key in ["correctAnswer", "correct_answer", "correctOption", "correct_option", "rightAnswer", "right_answer"]:
                                    if ans_key in obj:
                                        ans_val = obj[ans_key]
                                        if isinstance(ans_val, str) and len(ans_val) > 0:
                                            self._record_answer(q_text, ans_val)
                                        elif isinstance(ans_val, int):
                                            # Answer is an index, look for options
                                            for opt_fmt in [f"option{ans_val}", f"option_{ans_val}"]:
                                                if opt_fmt in obj:
                                                    self._record_answer(q_text, obj[opt_fmt])
                                                    break
                                            # Also check options array
                                            options = obj.get("options", [])
                                            if isinstance(options, list) and 0 <= ans_val - 1 < len(options):
                                                opt = options[ans_val - 1]
                                                opt_text = opt if isinstance(opt, str) else opt.get("text", "")
                                                if opt_text:
                                                    self._record_answer(q_text, opt_text)

                            # Strategy 3: Look for "answer" field with option index in same dict that has question
                            if q_text and "answer" in obj and not any(t in obj for t in ["amcq", "mcq", "msq"]):
                                ans_val = obj["answer"]
                                if isinstance(ans_val, str) and len(ans_val) > 1:
                                    self._record_answer(q_text, ans_val)
                                elif isinstance(ans_val, int):
                                    opt_key = f"option{ans_val}"
                                    if opt_key in obj:
                                        self._record_answer(q_text, obj[opt_key])

                            # Recurse into child values
                            for v in obj.values():
                                scan_for_answers(v, parent_question=q_text)
                        elif isinstance(obj, list):
                            for item in obj:
                                scan_for_answers(item, parent_question=parent_question)

                    scan_for_answers(data)

            except Exception:
                pass

        self.page.on("response", global_json_logger)
        logger.info("Browser started successfully.")

    def close(self):
        logger.info("Closing browser...")
        for resource in (self.page, self._browser, self._playwright):
            if resource:
                try:
                    resource.stop() if resource is self._playwright else resource.close()
                except Exception:
                    logger.debug("Browser resource was already closed.")
        logger.info("Browser closed.")

    def is_alive(self) -> bool:
        """Return True if the page/browser is still usable."""
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def _settle(self, timeout: int = 8000):
        """Wait for the page to settle. Uses domcontentloaded (reliable) and a
        best-effort networkidle that won't hang forever on media-streaming pages."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass  # media pages may never reach networkidle — that's fine

    def go_to(self, url: str):
        logger.info(f"Navigating to: {url}")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"go_to navigation warning: {e}")
        self._settle()

    def reload(self):
        """Reload the current page safely (avoids networkidle hangs on media pages)."""
        logger.info("Reloading page...")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"reload warning: {e}")
        self._settle()

    def go_to_base(self):
        self.go_to(self.browser_cfg.get("base_url", ""))

    def login(self) -> bool:
        logger.info("Attempting login...")
        try:
            self.go_to_base()
            login_sel = self.selectors.get("login", {})
            # The redesigned login opens on Mobile OTP rather than Email.
            self.page.locator("#mobile, " + login_sel["username_input"]).first.wait_for(state="visible")
            if not self.page.locator(login_sel["username_input"]).is_visible():
                self.page.get_by_role("button", name="Email", exact=True).click()
            if not self.creds.get("username") or not self.creds.get("password"):
                logger.error("Set BET_USERNAME and BET_PASSWORD in .env before running.")
                return False
            self.page.fill(login_sel["username_input"], self.creds["username"])
            self.page.fill(login_sel["password_input"], self.creds["password"])
            self.page.click(login_sel["signin_button"])
            # /verify is an intermediate redirect; networkidle alone can report
            # success while the account dashboard has not rendered yet.
            self.page.locator("a[href='#/home']").first.wait_for(state="visible")
            error_sel = login_sel.get("error_message", ".error-message")
            if self.page.is_visible(error_sel):
                error_text = self.page.inner_text(error_sel)
                logger.error(f"Login failed. Error: {error_text}")
                return False
            logger.info("Login successful!")
            return True
        except Exception as e:
            logger.error(f"Login exception: {e}")
            return False

    def select_answer_by_text(self, target_text: str, check_sel: str, max_tries: int = 8) -> bool:
        """Select the CORRECT MCQ option by its answer text, verifying the
        selection actually registered (Check button becomes enabled). The answer
        text can appear in several places (passage, question, option), so we try
        each matching element — preferring clickable, shortest matches (the option
        itself) — and stop as soon as Check is enabled. Never clicks a wrong option."""
        if not target_text or not target_text.strip():
            return False
        for i in range(max_tries):
            clicked = False
            try:
                clicked = bool(self.page.evaluate(
                    """(args) => {
                        const target = args[0], idx = args[1];
                        const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                        const t = norm(target);
                        if (!t) return false;
                        const W = window.innerWidth;
                        const cands = [];
                        const els = Array.from(document.querySelectorAll('div,li,label,[role=button],button,span,p'));
                        for (const el of els) {
                            const r = el.getBoundingClientRect();
                            if (r.left < 200 || r.top < 120) continue;   // skip sidebar/header
                            if (r.width < 30 || r.height < 14) continue;
                            const txt = norm(el.innerText);
                            if (!txt || txt.length > 220) continue;
                            if (txt.includes(t) || t.includes(txt)) {
                                const st = window.getComputedStyle(el);
                                const clickable = (st.cursor === 'pointer' || el.getAttribute('role') === 'button') ? 1 : 0;
                                cands.push({ el, len: txt.length, clickable });
                            }
                        }
                        // Prefer clickable elements, then the shortest text (the option, not the passage).
                        cands.sort((a, b) => (b.clickable - a.clickable) || (a.len - b.len));
                        if (idx < cands.length) { cands[idx].el.click(); return true; }
                        return false;
                    }""",
                    [target_text, i]
                ))
            except Exception:
                clicked = False
            if not clicked:
                break
            if self.is_check_enabled(check_sel, timeout=1200):
                logger.info(f"  ✅ Selected correct answer '{target_text[:40]}' (candidate #{i})")
                return True
        logger.warning(f"  ⚠️ Could not confirm selection of '{target_text[:40]}'")
        return False

    def is_check_enabled(self, check_sel: str, timeout: int = 2000) -> bool:
        """Return True if the Check button is visible AND enabled."""
        try:
            loc = self.page.locator(check_sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc.is_enabled()
        except Exception:
            return False

    def click_check(self, check_sel: str, timeout: int = 8000) -> bool:
        """Click the Check button only once it's enabled. Returns False if it
        never becomes enabled (instead of hanging 30s on a disabled button)."""
        try:
            loc = self.page.locator(check_sel).first
            start = time.time()
            while (time.time() - start) * 1000 < timeout:
                try:
                    if loc.is_visible() and loc.is_enabled():
                        loc.click(timeout=3000)
                        return True
                except Exception:
                    pass
                self.page.wait_for_timeout(300)
            return False
        except Exception:
            return False

    def select_option_until_check_enabled(self, check_sel: str, max_tries: int = 10) -> bool:
        """Click candidate answer options one-by-one until the Check button
        becomes enabled. Restricted to the main content area so it never clicks
        header chrome (streak flame) or sidebar items."""
        if self.is_check_enabled(check_sel, timeout=800):
            return True
        self.dismiss_popups()
        for i in range(max_tries):
            clicked = False
            try:
                clicked = bool(self.page.evaluate(
                    "(i) => { const opts = (" + self._OPTION_SCAN_JS + ")(); if (i < opts.length) { opts[i].click(); return true; } return false; }",
                    i
                ))
            except Exception:
                clicked = False
            if not clicked:
                break
            if self.is_check_enabled(check_sel, timeout=1200):
                logger.info(f"  Option #{i} selected — Check is now enabled.")
                return True
        return self.is_check_enabled(check_sel, timeout=500)

    def click(self, selector: str, wait_for_nav: bool = False, timeout: int = None):
        logger.debug(f"Clicking: {selector}")
        self.page.wait_for_selector(selector, state="visible", timeout=timeout)
        self.page.click(selector, timeout=timeout)
        if wait_for_nav: self._settle()

    def safe_click(self, selector: str, timeout: int = 4000) -> bool:
        """Click without ever throwing. Returns True if the click happened.
        Used for non-critical UI like exit/close buttons so a missing element
        never aborts the whole run."""
        try:
            self.page.click(selector, timeout=timeout)
            return True
        except Exception as e:
            logger.debug(f"safe_click skipped ({selector}): {e}")
            return False

    def fill_text(self, selector: str, text: str):
        logger.debug(f"Filling text into: {selector}")
        self.page.wait_for_selector(selector, state="visible")
        self.page.fill(selector, "")
        self.page.fill(selector, text)

    def get_text(self, selector: str, default: str = "") -> str:
        try:
            self.page.wait_for_selector(selector, state="visible", timeout=500)
            return self.page.inner_text(selector).strip()
        except Exception:
            return default

    def element_exists(self, selector: str, timeout: int = 5000) -> bool:
        try:
            self.page.wait_for_selector(selector, state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    def get_all_text_of(self, selector: str) -> list[str]:
        try:
            elements = self.page.query_selector_all(selector)
            return [el.inner_text().strip() for el in elements if el.inner_text().strip()]
        except Exception:
            return []

    def wait(self, milliseconds: int):
        self.page.wait_for_timeout(milliseconds)

    def setup_audio_interception(self):
        self._intercepted_audio_url = None
        self._audio_listener = lambda response: self._handle_audio_response(response)
        self.page.on("response", self._audio_listener)

    def _handle_audio_response(self, response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        is_audio = any(ext in url.lower() for ext in [".mp3", ".wav", ".ogg", ".webm", ".m4a", ".aac"]) or "audio/" in content_type
        if is_audio and response.status == 200:
            logger.info(f"🎵 Intercepted audio URL: {url}")
            self._intercepted_audio_url = url

    def get_audio_url_from_dom(self) -> str:
        try:
            element = self.page.query_selector("audio")
            if element:
                src = element.get_attribute("src")
                if src:
                    logger.info(f"🎵 Found audio URL in DOM: {src}")
                    return src
        except Exception:
            pass
        return None

    def get_intercepted_audio_url(self, wait_seconds: int = 8) -> str:
        start_time = time.time()
        while time.time() - start_time < wait_seconds:
            if self._intercepted_audio_url: return self._intercepted_audio_url
            self.page.wait_for_timeout(500)
        return None

    def remove_audio_interception(self):
        if hasattr(self, "_audio_listener"):
            self.page.remove_listener("response", self._audio_listener)
            delattr(self, "_audio_listener")
        self._intercepted_audio_url = None

    def dismiss_popups(self):
        """Press Escape to close any stray popup (e.g. the streak calendar) that
        may have opened, so it doesn't block option clicks."""
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)
        except Exception:
            pass

    # JS snippet that returns answer-option-like elements, restricted to the main
    # content area so it never hits header chrome (streak flame, account) or the
    # left sidebar (Home/AI Bet/Reports). Returns clickable option candidates.
    _OPTION_SCAN_JS = """
        () => {
            const navWords = ['check','submit','continue','next','exit','close','learn','practice',
                'play','record','skip','unlock','review','start','video','done'];
            const chromeWords = ['home','ai bet','ai fluentedge','reports','analytics','my account',
                'lessons','start learning','streak','earn streaks','monthly activity',
                'january','february','march','april','may','june','july','august','september',
                'october','november','december','sun','mon','tue','wed','thu','fri','sat'];
            const W = window.innerWidth, H = window.innerHeight;
            const out = [];
            const els = Array.from(document.querySelectorAll('div,li,label,[role=button],button'));
            for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.left < 200) continue;            // skip left sidebar
                if (r.top < 120) continue;             // skip top header
                if (r.width < 80 || r.height < 24) continue;
                if (r.width > W * 0.92) continue;      // skip full-width containers/passage
                const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!txt || txt.length < 2 || txt.length > 200) continue;
                if (!/[a-zA-Z]/.test(txt)) continue;   // must contain letters
                const low = txt.toLowerCase();
                if (navWords.some(w => low === w || low.startsWith(w + ' ') || low === w + '.')) continue;
                if (chromeWords.some(w => low.includes(w))) continue;
                const st = window.getComputedStyle(el);
                if (st.cursor === 'pointer' || el.getAttribute('role') === 'button') {
                    out.push(el);
                }
            }
            return out;
        }
    """

    def click_first_generic_option(self) -> bool:
        """Click the first 'option-like' element in the MAIN CONTENT area, without
        needing its CSS class. Avoids header chrome (streak flame) and sidebar."""
        self.dismiss_popups()
        try:
            return bool(self.page.evaluate(
                "() => { const opts = (" + self._OPTION_SCAN_JS + ")(); if (opts.length) { opts[0].click(); return true; } return false; }"
            ))
        except Exception as e:
            logger.debug(f"click_first_generic_option failed: {e}")
            return False

    def click_by_text(self, target_text: str) -> bool:
        """Click an option purely by its text content, without needing to know
        the option's CSS class. Uses Playwright's text locator, then walks up to
        a clickable ancestor if needed. This is the most reliable way to select
        MCQ options on pages whose markup we don't know in advance (e.g. Reading)."""
        if not target_text or not target_text.strip():
            return False
        snippet = target_text.strip()
        # Use a shortened, stable snippet (first ~6 words) to improve matching
        words = snippet.split()
        if len(words) > 8:
            snippet = " ".join(words[:8])
        try:
            loc = self.page.get_by_text(snippet, exact=False)
            count = loc.count()
            if count >= 1:
                target = loc.first
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                # Click the option container (parent) so frameworks register the
                # selection even if the text sits in a child node.
                try:
                    handle = target.element_handle()
                    if handle:
                        clicked = handle.evaluate(
                            """el => {
                                const clickable = el.closest('button, [role=button], li, label, div[class*=option], div[class*=cursor], div[class*=choice], div');
                                (clickable || el).click();
                                return true;
                            }"""
                        )
                        if clicked:
                            logger.info(f"Clicked option by text: '{snippet[:40]}'")
                            return True
                except Exception:
                    pass
                # Fallback: direct click on the located text element
                target.click(timeout=3000)
                logger.info(f"Clicked option by text (direct): '{snippet[:40]}'")
                return True
        except Exception as e:
            logger.debug(f"click_by_text failed: {e}")
        return False

    def dump_clickable_elements(self, limit: int = 40):
        """Diagnostic: log visible buttons and clickable-looking elements so we
        can see the real DOM when task detection fails."""
        try:
            info = self.page.evaluate(
                """() => {
                    const out = [];
                    const els = document.querySelectorAll('button, [role=button], a, div, li, label');
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 10 || r.height < 10) continue;
                        const txt = (el.innerText || '').trim().replace(/\\s+/g,' ');
                        if (!txt || txt.length > 80) continue;
                        out.push(el.tagName.toLowerCase() + '.' + (el.className||'').toString().split(' ').slice(0,2).join('.') + ' :: ' + txt.slice(0,60));
                        if (out.length >= 60) break;
                    }
                    return out;
                }"""
            )
            logger.info("🔍 DOM DUMP (clickable elements):")
            for line in info[:limit]:
                logger.info(f"    {line}")
        except Exception as e:
            logger.debug(f"dump_clickable_elements failed: {e}")

    def find_and_click_option(self, target_text: str) -> bool:
        import difflib
        # PRIMARY: selector-agnostic text-based click (works for Reading and any
        # page whose option markup we don't know).
        if self.click_by_text(target_text):
            return True

        option_selectors = self.selectors.get("exam", {}).get("option_selectors", [".option", "div[role='button']", ".choice"])

        target_clean = " ".join(target_text.strip().lower().split())
        best_match = None
        best_ratio = 0.0
        best_element = None

        for sel in option_selectors:
            elements = self.page.query_selector_all(sel)
            for el in elements:
                el_text = " ".join(el.inner_text().strip().lower().split())

                # Check exact or substring match first
                if target_clean in el_text or el_text in target_clean:
                    best_ratio = 1.0
                    best_element = el
                    best_match = el.inner_text().strip()
                    break

                # Otherwise, calculate similarity ratio
                ratio = difflib.SequenceMatcher(None, target_clean, el_text).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_element = el
                    best_match = el.inner_text().strip()

            if best_ratio == 1.0:
                break

        # If we have a decent match (> 50% similarity)
        if best_element and best_ratio > 0.5:
            try:
                logger.info(f"Found matching option (ratio {best_ratio:.2f}): '{best_match}' — clicking it.")
                best_element.click()
                return True
            except Exception:
                pass

        # Fallback: Just click the first option so we don't get permanently stuck
        for sel in option_selectors:
            elements = self.page.query_selector_all(sel)
            if elements:
                try:
                    logger.warning(f"No good match found for '{target_text}'. Clicking first option as fallback.")
                    elements[0].click()
                    return True
                except Exception:
                    pass

        return False

    def get_writing_prompt(self) -> str:
        prompt_selectors = [".question-text", ".writing-prompt", "p.prompt", ".task-description", "h3.question", ".exam-question"]
        for selector in prompt_selectors:
            text = self.get_text(selector)
            if text: return text

        # Fallback to smart block extraction
        try:
            blocks = self.page.evaluate(
                """() => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const out = [];
                    for (const el of document.querySelectorAll('p,h1,h2,h3,h4,h5,span,div')) {
                        const r = el.getBoundingClientRect();
                        if (r.left < 200 || r.top < 110) continue;

                        let hasDirectText = false;
                        for (let child of el.childNodes) {
                            if (child.nodeType === Node.TEXT_NODE && norm(child.textContent).length > 5) {
                                hasDirectText = true; break;
                            }
                        }
                        if (!hasDirectText) continue;

                        const txt = norm(el.innerText);
                        if (txt.length > 5 && txt.length < 500 && out.indexOf(txt) === -1) {
                            out.push(txt);
                        }
                    }
                    return out;
                }"""
            )
            ignore_phrases = ["check", "submit", "continue", "next", "start writing", "time left", "practice (", "my account", "words..."]
            filtered = [b for b in blocks if not any(b.lower() == ip for ip in ignore_phrases) and not any(ip in b.lower() for ip in ["practice (", "my account", "start writing", "time left", "max 90", "min 10", "words..."])]
            return " ".join(filtered)
        except Exception:
            return self.page.inner_text("body")[:1000]

    def get_reading_content(self) -> dict:
        result = {"passage": "", "question": "", "options": []}
        passage_selectors = [".passage", ".reading-text", ".article-text", "article", ".content-body"]
        for sel in passage_selectors:
            text = self.get_text(sel)
            if text:
                result["passage"] = text
                break
        question_selectors = ["p.text-base.font-medium", ".question", ".question-text", "h4.q-text", ".quiz-question"]
        for sel in question_selectors:
            text = self.get_text(sel)
            if text:
                result["question"] = text
                break
        option_selectors = self.selectors.get("exam", {}).get("option_selectors", [".option", "div[role='button']", ".choice"])
        for sel in option_selectors:
            texts = self.get_all_text_of(sel)
            if texts:
                result["options"] = texts
                break
        return result

    def get_known_answer(self, question_text: str) -> str:
        """Look up the correct answer from the globally intercepted network responses."""
        import difflib
        if not question_text or not self.known_answers:
            return None

        # Try exact match first
        if question_text in self.known_answers:
            return self.known_answers[question_text]

        # Try fuzzy match
        q_clean = " ".join(question_text.strip().lower().split())
        best_match = None
        best_ratio = 0.0

        for cached_q, cached_answer in self.known_answers.items():
            cached_clean = " ".join(cached_q.strip().lower().split())
            ratio = difflib.SequenceMatcher(None, q_clean, cached_clean).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = cached_answer

        if best_ratio > 0.7:
            logger.info(f"Fuzzy matched question (ratio {best_ratio:.2f})")
            return best_match

        return None

    def get_intercepted_answer(self, wait_seconds: int = 5) -> str:
        start_time = time.time()
        while time.time() - start_time < wait_seconds:
            if self._intercepted_correct_answer:
                return self._intercepted_correct_answer
            self.page.wait_for_timeout(500)
        return None

    def clear_intercepted_answer(self):
        self._intercepted_correct_answer = None
