# 🤖 AI Web Automation Chat

A Python web app that lets AI models (OpenAI, Claude, Gemini, OpenRouter, Local/Ollama) control a Playwright browser to automate any web task — form filling, scraping, PDF downloads, searching — all via natural language chat.

## ✨ Features

- **Multi-AI Provider** — OpenAI, Claude, Gemini, OpenRouter (free models supported), Local (Ollama / LM Studio)
- **Browser Automation** — Powered by Playwright Chromium; headless by default, visible mode optional
- **Smart Element Finding** — 7-strategy locator: role → text → aria-label → placeholder → label → title → CSS
- **Form Automation** — Fill, submit, extract confirmation/reference numbers automatically
- **PDF Scraping** — Find, download, and serve PDFs directly in chat
- **Screenshots on Demand** — Say "show me screenshot" — not shown automatically
- **Extended Reasoning** — OpenRouter thinking models (deepseek, gemma, etc.) supported
- **Persistent Settings** — API keys, model, headless toggle saved via UI

## 🚀 Quick Start (Windows)

```
Double-click start.bat
```

That's it. The batch file will:
1. Create a Python virtual environment
2. Install all dependencies
3. Install Playwright Chromium (~150MB, once)
4. Open `http://localhost:8000` in your browser

## ⚙️ Manual Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python server.py
```

Then open `http://localhost:8000`

## 🔑 Configuration

On first run, copy the settings template:
```bash
copy data\settings.template.json data\settings.json
```

Then configure your API keys in the ⚙️ Settings panel in the UI — or edit `data/settings.json` directly.

### Supported Providers

| Provider | Where to get key |
|---|---|
| OpenRouter | https://openrouter.ai (free tier available) |
| OpenAI | https://platform.openai.com |
| Anthropic (Claude) | https://console.anthropic.com |
| Google (Gemini) | https://aistudio.google.com |
| Local (Ollama) | No key needed — just run Ollama locally |

## 💬 Example Prompts

```
Go to google.com and search for "Playwright automation"
Fill the contact form at example.com with name=John, email=john@test.com
Download all PDFs from example.gov/reports
Navigate to amazon.com and find the price of a mechanical keyboard
```

## 🏗️ Architecture

```
start.bat / server.py         → Starts uvicorn with WindowsProactorEventLoop (required for Playwright)
app/main.py                   → FastAPI + WebSocket orchestration
app/ai_router.py              → Multi-provider AI streaming (OpenAI, Claude, Gemini, OpenRouter)
app/mcp_bridge.py             → Playwright browser bridge with smart element finding
app/settings_manager.py       → Persistent settings (API keys, model config)
app/file_manager.py           → Screenshot and PDF file management
static/index.html             → Chat UI
static/app.js                 → WebSocket client, settings panel
static/style.css              → Dark theme UI
```

## ⚠️ Windows Note

The server MUST be started via `server.py` (not `uvicorn` CLI directly).
This sets `WindowsProactorEventLoopPolicy` before uvicorn creates its event loop,
which is required for Playwright to spawn its browser subprocess on Windows.

## 📦 Requirements

- Python 3.10+
- Windows 10/11 (for `server.py` ProactorEventLoop fix)
- Internet connection (for AI APIs and browser automation)

## 📄 License

MIT
