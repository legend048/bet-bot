import yaml
import time
from modules.browser import BrowserController
import os
from dotenv import load_dotenv

load_dotenv()
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

config["credentials"] = {
    "username": os.getenv("BET_USERNAME"),
    "password": os.getenv("BET_PASSWORD")
}

if not all(config["credentials"].values()):
    raise RuntimeError("Set BET_USERNAME and BET_PASSWORD in .env before running this script.")

browser = BrowserController(config)
browser.start()
browser.login()
browser.go_to("https://corporate.bharatenglish.org/#/practice")
browser.wait(5000)

print("Starting a task...")
browser.click("h2:has-text('Listening') + p + button")
browser.wait(2000)
browser.click("button:has-text('Start')")
browser.wait(2000)
browser.click("button:has-text('Start Learning')")
browser.wait(2000)
browser.click("button:has-text('Practice')")
browser.wait(5000)

print("Playing through the exam...")
for i in range(5):
    print(f"Question {i+1}...")
    browser.wait(2000)
    browser.click(".option, div[role='button'], .choice")
    browser.wait(1000)
    browser.click("button:has-text('Check')")
    browser.wait(2000)
    browser.click("button:has-text('Continue'), button:has-text('Submit'), button:has-text('Next')")
    browser.wait(2000)

print("Exam finished. Current URL:", browser.page.url)
print("Refreshing page...")
browser.page.reload()
browser.wait(5000)
print("URL after refresh:", browser.page.url)

browser.close()
