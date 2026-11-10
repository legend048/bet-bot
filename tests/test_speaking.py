import logging
import base64
import io
import math
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from main import detect_task_type, parse_args, run_two_pass_test, submit_lesson
from modules.browser import BrowserController
from modules.speaker import Speaker
from tasks.adaptive import complete_learn, complete_video, process_adaptive_skill
from tasks.speaking import handle_speaking_task


class SpeakingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = BrowserController({"browser": {"headless": True, "timeout": 3000}})
        cls.browser.start()
        cls.page = cls.browser.page
        cls.page.route("https://bot.test/**", lambda route: route.fulfill(body="<html><body></body></html>", content_type="text/html"))
        cls.page.goto("https://bot.test/")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def test_login_switches_from_mobile_to_email(self):
        html = """<input id=mobile><button onclick="document.querySelector('#email').hidden=false;document.querySelector('#mobile').remove()">Email</button>
        <div id=email hidden><input id=username><input id=password type=password>
        <button onclick="document.body.innerHTML='<a href=&#34;#/home&#34;>Home</a>'">Sign in</button></div>"""
        self.browser.selectors = {"login": {"username_input": "#username", "password_input": "#password", "signin_button": "button:has-text('Sign in')"}}
        self.browser.creds = {"username": "person@example.test", "password": "fixture-only"}
        with patch.object(self.browser, "go_to_base", side_effect=lambda: self.page.set_content(html)):
            self.assertTrue(self.browser.login())

    def test_speaking_audio_preview_is_not_listening(self):
        self.page.set_content('<audio controls></audio><button aria-label="Start recording">Record</button><button>Check</button>')
        self.assertEqual(detect_task_type(self.browser, indicator_timeout=50), "speaking")

    def test_speaking_question_is_recorded_before_disabled_submit(self):
        self.page.set_content('<main><button aria-label="Start recording">Record</button><button disabled>Submit</button></main>')
        self.assertEqual(detect_task_type(self.browser, indicator_timeout=50), 'speaking')

    def test_injected_speech_records_audio_and_cleans_up(self):
        self.page.goto("https://bot.test/")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GROQ_API_KEY": "fixture-only"}):
            speaker = Speaker({"paths": {"temp_audio_dir": folder}, "browser": {"virtual_mic_file": str(Path(folder) / "mic.wav")}})
            audio_path = str(Path(folder) / "speech.wav")
            with wave.open(audio_path, "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"".join(struct.pack('<h', int(9000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(8000)))
            self.assertTrue(speaker.prepare_in_browser(self.page, audio_path))
            self.page.evaluate("""async () => {
                const stream = await navigator.mediaDevices.getUserMedia({audio:true});
                window.recorder = new MediaRecorder(stream);
                window.chunks = [];
                recorder.ondataavailable = event => chunks.push(event.data);
                recorder.start();
            }""")
            self.assertTrue(speaker.play_in_browser(self.page))
            size = self.page.evaluate("""() => new Promise(resolve => {
                recorder.onstop = () => resolve(new Blob(chunks).size);
                recorder.stop();
            })""")
            self.assertGreater(size, 1000)
            self.assertTrue(speaker.cleanup_in_browser(self.page))
            self.assertTrue(self.page.evaluate("window.injectedAudioStream === null && window.injectedAudioContext === null"))

    def test_adaptive_flow_skips_done_lessons_and_verifies_new_completion(self):
        topics = '<span>Course Topics</span><div class=card role=button onclick="openLesson()"><h3>Assigned Topic</h3>1 of 2 lessons</div>'
        done = '<section class=card><h3>Completed lesson</h3><button disabled>Done</button></section>'
        pending = '<section class=card><h3>Next lesson</h3><button aria-label="Video, Next lesson, done"></button><button aria-label="Learn, Next lesson, done"></button><button aria-label="Start practice, Next lesson">Practice</button></section>'
        lessons = done + pending
        completed = []
        self.page.goto("https://bot.test/")
        self.page.set_content(topics)

        def navigate(url):
            self.page.set_content(done + '<section class=card><h3>Next lesson</h3><button disabled>Done</button></section>' if completed else topics)
            self.page.evaluate("html => window.openLesson = () => document.body.innerHTML = html", lessons)

        with patch.object(self.browser, 'go_to', side_effect=navigate):
            count = process_adaptive_skill(self.browser, {"run": {"max_lessons": 1, "skip_videos": True}}, logging.getLogger('test'), lambda: completed.append(True))
        self.assertEqual(count, 1)
        self.assertEqual(completed, [True])

    def test_speaking_cli_limit(self):
        args = parse_args(['--skill', 'speaking', '--max-lessons', '1'])
        self.assertEqual((args.skill, args.max_lessons), ('speaking', 1))

    def test_final_summary_is_submitted_before_returning(self):
        self.page.set_content('<button hidden>Submit query</button><main>Practice 5/5<button hidden>Submit</button><button onclick="document.querySelector(\'main\').innerHTML=\'Lesson completed\'">Submit</button></main>')
        config = {"selectors": {"exam": {"submit_button": "button:text-is('Submit')"}}}
        with patch('main.detect_task_with_retry', return_value='unknown'):
            run_two_pass_test(self.browser, None, None, None, config, Mock())
        self.assertEqual(self.page.locator('main').inner_text(), 'Lesson completed')

    def test_submit_summary_is_detected_without_question_controls(self):
        self.page.set_content('<button hidden>Submit query</button><main>Practice 5/5<button>Submit</button></main>')
        self.assertEqual(detect_task_type(self.browser), 'submit')

    def test_actually_disabled_submit_is_not_forced(self):
        self.page.set_content('<main><button disabled onclick="window.submitted=true">Submit</button></main>')
        self.page.evaluate('window.submitted=false')
        with self.assertRaisesRegex(RuntimeError, 'visible lesson Submit button is disabled'):
            submit_lesson(self.browser, Mock(), timeout=100)
        self.assertFalse(self.page.evaluate('window.submitted'))

    def test_submit_uses_already_open_confirmation_dialog(self):
        self.page.set_content('''<main aria-hidden="true"><button onclick="window.wrongTarget=true">Submit</button></main>
        <div role="dialog" style="position:fixed;inset:0;background:white">
        Ready to submit?<button onclick="window.submissions++; setTimeout(()=>document.body.innerHTML='Lesson completed',250)">Submit</button></div>''')
        self.page.evaluate('window.wrongTarget=false; window.submissions=0')
        run_two_pass_test(self.browser, None, None, None, {"selectors": {"exam": {}}}, Mock())
        self.assertFalse(self.page.evaluate('window.wrongTarget'))
        self.assertEqual(self.page.evaluate('window.submissions'), 1)
        self.assertEqual(self.page.locator('body').inner_text(), 'Lesson completed')

    def test_submit_handles_dialog_opened_by_initial_click(self):
        self.page.set_content('''<main><button onclick="openConfirmation()">Submit</button></main>''')
        self.page.evaluate("""() => {
            window.submissions = 0;
            window.openConfirmation = () => {
                document.querySelector('main').setAttribute('aria-hidden', 'true');
                const dialog = document.createElement('div');
                dialog.setAttribute('role', 'dialog');
                dialog.textContent = 'Ready to submit?';
                const submit = document.createElement('button');
                submit.textContent = 'Submit';
                submit.onclick = () => { window.submissions++; document.body.innerHTML = 'Lesson completed'; };
                dialog.appendChild(submit);
                document.body.appendChild(dialog);
            };
        }""")
        submit_lesson(self.browser, Mock(), timeout=2000)
        self.assertEqual(self.page.evaluate('window.submissions'), 1)
        self.assertEqual(self.page.locator('body').inner_text(), 'Lesson completed')

    def test_failed_audio_preparation_never_starts_or_submits_recording(self):
        self.page.set_content('<button aria-label="Start recording" onclick="window.started=true">Record</button><button onclick="window.submitted=true">Continue</button>')
        self.page.evaluate('window.started = false; window.submitted = false')
        speaker = Mock()
        speaker.text_to_speech.return_value = 'fixture.wav'
        speaker.prepare_in_browser.return_value = False
        ai = Mock()
        ai.generate_speech_text.return_value = 'This is my colleague.'
        config = {"selectors": {"exam": {"check_button": "button:text-is('Check')", "continue_button": "button:text-is('Continue')", "submit_button": "button:text-is('Submit')"}}}
        with patch.object(self.browser, 'get_speaking_info', return_value=['EXTEMPORE', 'Introduce a colleague to the team.']):
            self.assertFalse(handle_speaking_task(self.browser, ai, speaker, config, Mock()))
        self.assertFalse(self.page.evaluate('window.started || window.submitted'))
        self.assertEqual(speaker.cleanup_in_browser.call_count, 2)

    def test_learn_scrolls_notes_and_uses_completion_dialog(self):
        self.page.set_content('<section class=card><button aria-label="Learn, Lesson, to do" onclick="openNotes()">Learn</button></section>')
        notes = """<main style='height:100px;overflow:auto' onscroll="document.querySelector('#finish').disabled=false">
        <h2>Reading Material</h2><p style='height:500px'>These are the full lesson notes.</p></main>
        <button id=finish disabled onclick="document.body.innerHTML='<button onclick=window.practiceOpened=true>Go to practice</button>'">I've finished reading</button>"""
        self.page.evaluate('html => {window.practiceOpened=false; window.openNotes=()=>document.body.innerHTML=html}', notes)
        opened = complete_learn(self.page, self.page.locator('section.card'), {"run": {"learn_seconds": 0}}, Mock())
        self.assertTrue(opened)
        self.assertTrue(self.page.evaluate('window.practiceOpened'))

    def test_video_skip_seeks_and_allows_natural_completion(self):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x00' * 16000 * 10)
        source = base64.b64encode(audio.getvalue()).decode('ascii')
        self.page.set_content('<section class=card><h3>Lesson</h3><button id=video aria-label="Video, Lesson, to do" onclick="openVideo()">Video</button><button id=practice disabled>Practice</button></section>')
        player = f'''<video src="data:audio/wav;base64,{source}" onseeked="parent.sawSeek=true" onended="parent.document.querySelector('#practice').disabled=false"></video><button onclick="document.querySelector('video').play()">Play</button>'''
        self.page.evaluate("""markup => {
            window.sawSeek = false;
            window.openVideo = () => {
                const dialog = document.createElement('div');
                const iframe = document.createElement('iframe');
                iframe.title = 'Video Lesson';
                iframe.srcdoc = markup;
                dialog.appendChild(iframe);
                const close = document.createElement('button');
                close.setAttribute('aria-label', 'Close video dialog');
                close.textContent = 'Close';
                close.onclick = () => dialog.remove();
                dialog.appendChild(close);
                document.body.appendChild(dialog);
            };
        }""", player)
        complete_video(self.page, self.page.locator('section.card'), {"run": {"skip_videos": True, "video_timeout": 15}}, Mock())
        self.assertTrue(self.page.evaluate('window.sawSeek'))
        self.assertTrue(self.page.locator('#practice').is_enabled())
        self.assertEqual(self.page.locator('iframe').count(), 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows SAPI only')
    def test_offline_speech_accepts_smart_quotes(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GROQ_API_KEY": "fixture-only"}):
            path = str(Path(folder) / "speech.wav")
            speaker = Speaker({"paths": {"temp_audio_dir": folder}, "browser": {"virtual_mic_file": path}})
            self.assertEqual(speaker.generate_speech_wav("I’m ready to describe my colleague’s work.", path), path)
            self.assertGreater(speaker.get_wav_duration(path), 1)


if __name__ == '__main__':
    unittest.main()
