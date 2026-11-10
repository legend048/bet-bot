# 🤖 Bharat English Corporate Automation Bot — Implementation Plan

This document outlines the implementation plan for an automated bot designed to interact with the Bharat English Corporate platform. The bot will log in, navigate to various assignment sections (Writing, Reading, Listening, Speaking), process questions, generate answers using AI, and submit them, while also observing the UI/UX.

## 🧠 Full Pipeline

The core pipeline involves browser automation, AI-powered question answering, and interaction with the platform's UI. The flow is as follows:

```
LOGIN → NAVIGATE TO ASSIGNMENTS → DETECT TASK TYPE → PROCESS TASK (AI-ASSISTED) → SUBMIT ANSWER → LOGOUT
```

For specific task types:

-   **WRITING**: Read prompt → AI generates text response → Fill textarea → Submit.
-   **READING**: Read passage and question → AI selects correct option from choices → Click option → Submit.
-   **LISTENING**: Play audio → Transcribe audio (future enhancement) → AI selects correct option from choices → Click option → Submit.
-   **SPEAKING**: Read prompt → AI generates speech (future enhancement) → Simulate speaking (future enhancement) → Submit.

## 📦 Project Structure

The project will be organized into a main directory `bharat_bot` with the following structure:

```
bharat_bot/
├── main.py                ← Orchestrates the entire automation flow
├── .env                   ← Stores sensitive information like API keys
├── config.yaml            ← Centralized configuration for selectors and settings
├── requirements.txt       ← Lists all Python dependencies
├── README.md              ← Project documentation and usage instructions
│
└── modules/               ← Contains modular components for browser, AI, etc.
    ├── browser.py         ← Playwright browser controller
    └── ai_answerer.py     ← Handles AI interactions for generating answers
    # Future modules for audio capture, transcription, and text-to-speech
```

## ⚙️ Dependencies

The following Python libraries and tools are required:

| Dependency         | Purpose                                     |
| :----------------- | :------------------------------------------ |
| `playwright`       | Headless browser automation                 |
| `openai`           | AI model interaction (GPT-4o for answers)   |
| `pyyaml`           | Configuration file parsing                  |
| `python-dotenv`    | Loading environment variables               |
| `beautifulsoup4`   | (Future) HTML parsing for complex extraction|

Installation commands:

```bash
pip install -r requirements.txt
playwright install chromium
```

## 🔑 Configuration

### `.env` file

This file will store the `OPENAI_API_KEY` for secure access to the OpenAI API. It should be placed in the root of the `bharat_bot` directory.

```
OPENAI_API_KEY=sk-your_openai_key_here
```

### `config.yaml`

This YAML file centralizes all configurable parameters, including browser settings, user credentials, and UI selectors. This makes the bot adaptable to potential UI changes and different user accounts.

```yaml
browser:
  headless: false
  slow_mo: 50
  base_url: "https://corporate.bharatenglish.org"

credentials:
  username: "your_bharat_english_username_or_email"
  password: "your_bharat_english_password"

selectors:
  login:
    username_input: "#username"
    password_input: "#password"
    signin_button: "button:has-text(\'Sign in\')"

  navigation:
    sidebar_home: "a:has-text(\'Home\')"
    sidebar_ai_bet: "a:has-text(\'AI Bet\')"
    sidebar_ai_fluentedge: "a:has-text(\'AI FluentEdge\')"
    sidebar_reports: "a:has-text(\'Reports\')"

  fluentedge:
    listening_start: "h2:has-text(\'Listening\') + p + button"
    speaking_start: "h2:has-text(\'Speaking\') + p + button"
    reading_start: "h2:has-text(\'Reading\') + p + button"
    writing_start: "h2:has-text(\'Writing\') + p + button"

  levels:
    start_button: "button:has-text(\'Start\')"
    revisit_button: "button:has-text(\'Revisit\')"

  units:
    start_learning: "button:has-text(\'Start Learning\')"
    review: "button:has-text(\'Review\')"

  lessons:
    practice_button: "button:has-text(\'Practice\')"
    learn_button: "button:has-text(\'Learn\')"

  exam:
    textarea: "textarea[placeholder*=\'Start writing\']"
    check_button: "button:has-text(\'Check\')"
    continue_button: "button:has-text(\'Continue\')"
    close_button: "button[hint=\'Close practice\']"
    exit_button: "button:has-text(\'Exit\')"
    play_audio: "button[hint=\'Play audio\']"
    option: ".option, div[role=\'button\'], .choice"

ai:
  model: "gpt-4o"
```

## 🗂️ Module Details

### `modules/browser.py`

This module encapsulates all Playwright browser interactions. It provides methods for:

-   **Initialization**: Launching a Chromium browser instance (headless or with UI).
-   **Navigation**: Going to specific URLs.
-   **Login**: Automating the login process using configured credentials.
-   **Interaction**: Clicking elements, filling text fields, and waiting for page states.
-   **Text Extraction**: Retrieving text content from specified selectors.
-   **Closure**: Closing the browser instance.

### `modules/ai_answerer.py`

This module handles all AI-related functionalities, primarily using the OpenAI API (configured to use `gpt-4o` as per `config.yaml`). It includes methods for:

-   **Writing Task Answering**: Generating a comprehensive text response based on a given prompt.
-   **MCQ Answering**: Analyzing a question and a list of options to select the most appropriate answer.

### `main.py`

This is the main orchestrator of the bot. It ties together the `Browser` and `AIAnswerer` modules to execute the automation flow. Its responsibilities include:

-   Initializing browser and AI components.
-   Performing login.
-   Navigating through the platform to reach assignment sections.
-   Identifying the type of assignment (Writing, Reading, Listening, Speaking).
-   Invoking the appropriate AI answering mechanism.
-   Submitting answers and progressing through tasks.
-   Includes basic error handling and logging.

## 🚀 Usage

To run the bot, ensure all dependencies are installed and configuration files (`.env`, `config.yaml`) are correctly set up. Then, execute `main.py`:

```bash
python main.py
```

## 📈 Future Enhancements

-   **Dynamic Task Detection**: Implement more robust logic to dynamically detect task types (Listening, Speaking, Reading, Writing) and their specific UI elements.
-   **Audio Processing**: Integrate `sounddevice` and `Groq Whisper API` for real-time audio capture and transcription for Listening tasks, and `edge-tts` for text-to-speech for Speaking tasks.
-   **Advanced UI Interaction**: Utilize `BeautifulSoup` for more complex HTML parsing and data extraction, especially for dynamic content or intricate table structures.
-   **Error Handling & Reporting**: Enhance error handling with more specific exception types and detailed logging, potentially integrating with a reporting mechanism.
-   **Modular Task Handlers**: Create separate functions or classes for each assignment type (e.g., `WritingTaskHandler`, `ListeningTaskHandler`) to improve code organization and maintainability.
-   **Parallel Processing**: Explore options for parallelizing tasks where applicable, though this is less common in sequential browser automation.
-   **Robust Selector Management**: Implement a mechanism to automatically update or suggest new selectors if the UI changes, possibly using visual recognition or more advanced DOM analysis.
-   **Comprehensive Logging**: Implement a more detailed logging system to track bot actions, AI responses, and submission results.

This plan provides a solid foundation for building a robust automation solution for the Bharat English Corporate platform. The modular design allows for incremental development and easy integration of future features. [1]

## References

[1]: User-provided `IMPLEMENTATION_PLAN_1.md` (local file) - Initial implementation plan and requirements.
