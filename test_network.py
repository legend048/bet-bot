import yaml
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

print("Logging in...")
if not browser.login():
    print("Login failed!")
    exit(1)

print("Navigating to practice...")
browser.go_to("https://corporate.bharatenglish.org/#/practice")
browser.wait(5000)

print("Looking for listening task...")
browser.click("h2:has-text('Listening') + p + button")
browser.wait(2000)
browser.click("button:has-text('Start')")
browser.wait(2000)
browser.click("button:has-text('Start Learning')")
browser.wait(2000)
browser.click("button:has-text('Practice')")
browser.wait(5000)

print("Setting up global interceptor...")
os.makedirs("intercept", exist_ok=True)

def handle_response(response):
    try:
        url = response.url
        if "corporate.bharatenglish.org" not in url and "wexledu" not in url:
            return

        print(f"Captured response: {url} | Status: {response.status}")
        try:
            body = response.body().decode("utf-8")
            with open(f"intercept/{url.replace('/', '_').replace(':', '_')[-50:]}.txt", "w", encoding="utf-8") as f:
                f.write(f"URL: {url}\nHeaders: {response.headers}\n\n{body}")
        except Exception as e:
            pass
    except Exception:
        pass

browser.page.on("response", handle_response)

print("Clicking first option...")
browser.click(".option, div[role='button'], .choice")
browser.wait(5000)
print("Done. Check intercept folder.")
browser.close()
