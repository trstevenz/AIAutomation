"""
AI Router — abstracts OpenAI, Anthropic Claude, Google Gemini, OpenRouter, and Local AI.
Features: smart error parsing, auto-retry with backoff on 429/503, friendly messages.
"""
import json
import asyncio
import re
import time
from typing import Callable

import httpx

SYSTEM_PROMPT = """
You are an AI web automation assistant. You help users automate browser tasks.

BEHAVIOR:
- If the user asks a general knowledge question (not related to web automation), briefly answer in 1-2 sentences, then offer 2-3 specific web automation things you can do for them instead.
- If the user asks to do a web task: execute it IMMEDIATELY, silently, without explaining your steps.
- NEVER describe what tools you are calling. NEVER say "I will navigate to" or "I am clicking". Just DO it.
- NEVER apologize or explain why something is hard. Just attempt it.
- NEVER suggest the user do it manually.

FINAL RESPONSE RULE (CRITICAL):
- After EVERY completed task (success or failure), you MUST send a final natural-language response.
- NEVER end silently. Always say what happened.
- Success example: "Done! Searched Google for 'Playwright automation' — top result is playwright.dev."
- Failure example: "YouTube blocked the automated browser. Want me to try 1) DuckDuckGo video search, 2) Bing Videos, or 3) direct yt-dlp download?"
- Always be specific: mention URLs visited, data found, form fields filled, errors encountered.

RESPONSE FORMAT:
- While working: say NOTHING (tools run silently).
- When done: give ONE short natural language summary.
- For errors: say what happened in plain English. Never show raw error messages.

SCREENSHOT RULE:
- NEVER call the screenshot tool unless the user explicitly says: "show me", "screenshot", "what does it look like".

TOKEN-EFFICIENT PAGE READING (IMPORTANT — saves money and is faster):
- Use `get_accessibility_snapshot` to understand page structure BEFORE clicking.
  It returns ~200-400 tokens of structured element info vs thousands of tokens for HTML.
  It shows: buttons, inputs, links, headings — exactly what you need to navigate.
- Only use `get_text` for reading page content (articles, search results, etc.).
- NEVER use `get_html` — it wastes thousands of tokens on raw markup.
- Workflow: navigate → get_accessibility_snapshot → click/fill based on snapshot → done.

AUTOMATION APPROACH:
- Use human-readable element names from the accessibility snapshot.
- For search: navigate → get_accessibility_snapshot → fill search box → press Enter.
- For forms: navigate → get_accessibility_snapshot → fill each field by label → submit.
- If an element isn't found, try alternative text or approach silently.

WHEN A SITE BLOCKS AUTOMATION (blocked: true in tool result):
- Tell the user in ONE sentence which site blocked and why.
- Immediately offer 2-3 specific alternatives as a numbered list.
- Ask: "Which option would you like me to try?"
- Wait for user choice, then proceed IMMEDIATELY.
- Never just stop — always have a backup plan.

SCOPE: You ONLY automate web browsers. For anything else, briefly help then redirect to web automation.
"""

PLANNER_SYSTEM_PROMPT = """
You are an AI web automation planner. Your job is to analyze the user's request, execution history, and current page state, and decide the next steps.

You must respond with a JSON object. Do not output any other text before or after the JSON.

JSON SCHEMA:
{
  "response": "Use this field ONLY if the task is finished/failed, or if the user asked a general question. Provide a natural-language reply here. Otherwise, leave this field empty (\"\").",
  "plan": [
    {
      "tool": "tool_name",
      "args": { ... }
    }
  ],
  "done": false
}

RULES:
1. If the user's prompt is a general knowledge question or greeting (not asking for a browser task):
   - Set "response" to a brief 1-2 sentence answer, followed by 2-3 bullet points suggesting specific web automation tasks you can perform (e.g. searching, form filling, downloading).
   - Set "plan" to [].
   - Set "done" to true.

2. If the user wants to perform a browser/web task:
   - If the task is not yet started:
     - Set "plan" to the initial steps (e.g., navigate, fill search, etc.).
     - Set "response" to "".
     - Set "done" to false.
   - If the task is in progress (you will see the execution log and current page state):
     - If the goal is achieved:
       - Set "plan" to [].
       - Set "response" to "Done! [short summary of what was accomplished]".
       - Set "done" to true.
     - If the page is blocked (e.g., Cloudflare, CAPTCHA, access denied):
       - Set "plan" to [].
       - Set "response" to a message stating the site blocked automation, and list 2-3 alternative websites/options the user might want you to try.
       - Set "done" to true.
     - Otherwise, output the next steps in "plan" to continue the task. Set "response" to "" and "done" to false.

3. Available tools are:
   - navigate (url)
   - click (selector, text, x, y)
   - fill (selector, value)
   - select_option (selector, value)
   - type_text (selector, text)
   - get_text (selector)
   - get_accessibility_snapshot (interesting_only)
   - wait_for_selector (selector, timeout)
   - submit_form (selector)
   - scroll (direction, amount)
   - go_back ()
   - screenshot (full_page)
   - find_links (pattern)
   - find_pdfs ()
   - download_pdf (url, filename)
   - print_to_pdf (filename)
   - execute_js (script)

4. Keep plans compact and focused (1-4 steps at a time). Prefer using get_accessibility_snapshot to inspect pages.
"""

_last_request_time = 0.0
_request_lock = asyncio.Lock()


async def enforce_cooldown(min_interval: float = 3.0):
    """Enforce a minimum interval between consecutive API requests to avoid rate limits."""
    global _last_request_time
    async with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < min_interval:
            wait_time = min_interval - elapsed
            await asyncio.sleep(wait_time)
        _last_request_time = time.monotonic()


# ─── Retry config ───────────────────────────────────────────
MAX_RETRIES    = 3          # max attempts on 429 / 503
RETRY_DELAYS   = [5, 15, 30]  # seconds between retries


# ─── Error parser ────────────────────────────────────────────
def parse_api_error(status_code: int, body: str, provider: str = "") -> str:
    """Turn a raw API error body into a short, human-friendly message."""
    # Try to extract nested message from JSON
    try:
        data = json.loads(body)
        # OpenAI / OpenRouter style
        err = data.get("error", {})
        if isinstance(err, dict):
            msg = err.get("message", "")
            # OpenRouter wraps upstream errors in metadata.raw
            meta_raw = err.get("metadata", {}).get("raw", "")
            if meta_raw:
                msg = meta_raw          # use the upstream reason, it's more specific
        elif isinstance(err, str):
            msg = err
        else:
            msg = data.get("message", body)
    except Exception:
        msg = body.strip()

    # Trim very long messages
    if len(msg) > 300:
        msg = msg[:300] + "…"

    # Map status codes to friendly prefixes
    if status_code == 429:
        return (
            f"[Rate Limited 429] {msg}\n\n"
            "**What to do:**\n"
            "- Wait a moment and send your message again\n"
            "- Switch to a different model in Settings\n"
            "- Add your own API key to get higher limits\n"
            "- For OpenRouter free models, add credits at openrouter.ai"
        )
    if status_code == 401:
        return (
            f"[Unauthorised 401] Invalid or missing API key: {msg}\n\n"
            "Go to Settings and enter your API key for this provider."
        )
    if status_code == 403:
        return f"[Forbidden 403] {msg}\n\nCheck your API key permissions."
    if status_code == 402:
        return (
            f"[Payment Required 402] {msg}\n\n"
            "Your account may have run out of credits. Check your billing."
        )
    if status_code in (500, 502, 503, 504):
        return (
            f"[Server Error {status_code}] {msg}\n\n"
            "The AI provider is having issues. Please retry in a moment."
        )
    return f"[API Error {status_code}] {msg}"


# ─── Retry wrapper ───────────────────────────────────────────
async def _with_retry(fn, on_chunk: Callable, retryable=(429, 500, 502, 503, 504)):
    """Call async fn(); on retryable status, wait and retry with backoff."""
    for attempt in range(MAX_RETRIES):
        status, result = await fn()
        if status == 200:
            return result
        if status in retryable and attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAYS[attempt]
            if on_chunk:
                await on_chunk(
                    f"\n⏳ Got **{status}** — retrying in {delay}s "
                    f"(attempt {attempt + 2}/{MAX_RETRIES})…\n"
                )
            await asyncio.sleep(delay)
        else:
            return result   # final error string already set by caller
    return ""


# ─── OpenAI-compatible streaming ─────────────────────────────
async def stream_openai_compatible(
    messages: list,
    api_key: str,
    base_url: str,
    model: str,
    tools: list = None,
    on_chunk: Callable = None,
    on_tool_call: Callable = None,
    extra_headers: dict = None,
    use_reasoning: bool = False,   # OpenRouter extended reasoning
    json_mode: bool = False,
) -> tuple:
    """
    Stream from any OpenAI-compatible endpoint.
    Returns (full_response_text, reasoning_details_list | None)
    reasoning_details must be stored in the assistant message for multi-turn continuity.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    # Normalise messages — convert 'tool' role to user message
    # Also pass back reasoning_details in assistant messages unchanged
    clean_messages = []
    for m in messages:
        if m.get("role") == "tool":
            clean_messages.append({
                "role": "user",
                "content": f"[Tool result]: {m.get('content', '')}"
            })
        else:
            msg_copy = {"role": m["role"], "content": m.get("content", "")}
            # Preserve reasoning_details for OpenRouter multi-turn reasoning
            if "reasoning_details" in m and m["reasoning_details"]:
                msg_copy["reasoning_details"] = m["reasoning_details"]
            clean_messages.append(msg_copy)

    payload = {
        "model": model,
        "messages": clean_messages,
        "stream": True,
        "temperature": 0.7,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if use_reasoning:
        payload["reasoning"] = {"enabled": True}

    url = f"{base_url.rstrip('/')}/chat/completions"

    for attempt in range(MAX_RETRIES):
        full_response     = ""
        tool_calls_buffer = {}
        reasoning_details = []    # accumulated reasoning_details blocks
        thinking_buffer   = ""   # current thinking text chunk

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode()
                        friendly = parse_api_error(resp.status_code, body)

                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAYS[attempt]
                            if on_chunk:
                                await on_chunk(
                                    f"\n**Rate limited ({resp.status_code})** -- retrying in **{delay}s** "
                                    f"(attempt {attempt + 2}/{MAX_RETRIES})...\n"
                                )
                            await asyncio.sleep(delay)
                            continue

                        if on_chunk:
                            await on_chunk(f"\n{friendly}\n")
                        return friendly, None

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data   = json.loads(data_str)
                            choice = data.get("choices", [{}])[0]
                            delta  = choice.get("delta", {})

                            # ── Regular content ──────────────────────
                            if "content" in delta and delta["content"]:
                                chunk = delta["content"]
                                full_response += chunk
                                if on_chunk:
                                    await on_chunk(chunk)

                            # ── Reasoning / thinking chunks ──────────
                            # OpenRouter streams reasoning as delta.reasoning or
                            # inside delta.content with type="thinking"
                            if "reasoning" in delta and delta["reasoning"]:
                                thinking_buffer += delta["reasoning"]

                            # Some models stream reasoning_details blocks
                            if "reasoning_details" in delta:
                                for rd in delta["reasoning_details"]:
                                    reasoning_details.append(rd)

                            # ── Tool calls ───────────────────────────
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_buffer:
                                        tool_calls_buffer[idx] = {
                                            "id": tc.get("id", f"call_{idx}"),
                                            "name": tc.get("function", {}).get("name", ""),
                                            "arguments": "",
                                        }
                                    if tc.get("id"):
                                        tool_calls_buffer[idx]["id"] = tc["id"]
                                    fn = tc.get("function", {})
                                    if fn.get("name"):
                                        tool_calls_buffer[idx]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        tool_calls_buffer[idx]["arguments"] += fn["arguments"]
                        except Exception:
                            continue

                    # Build final reasoning_details from buffer if not already streamed
                    if thinking_buffer and not reasoning_details:
                        reasoning_details = [{"type": "thinking", "thinking": thinking_buffer}]

        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                if on_chunk:
                    await on_chunk(f"\nRequest timed out -- retrying in {delay}s...\n")
                await asyncio.sleep(delay)
                continue
            if on_chunk:
                await on_chunk("\n**Request timed out** after multiple attempts. Please try again.\n")
            return "", None

        # Fire tool calls after successful stream
        if tool_calls_buffer and on_tool_call:
            for idx in sorted(tool_calls_buffer.keys()):
                tc = tool_calls_buffer[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except Exception:
                    args = {}
                await on_tool_call(tc["id"], tc["name"], args)

        return full_response, reasoning_details or None

    return "", None


# ─── Claude streaming ─────────────────────────────────────────
async def stream_claude(
    messages: list,
    api_key: str,
    model: str,
    tools: list = None,
    on_chunk: Callable = None,
    on_tool_call: Callable = None,
    json_mode: bool = False,
) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    system_msg = SYSTEM_PROMPT
    claude_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        elif m["role"] == "tool":
            claude_messages.append({
                "role": "user",
                "content": f"[Tool result]: {m.get('content', '')}"
            })
        else:
            claude_messages.append({"role": m["role"], "content": m.get("content", "")})

    claude_tools = None
    if tools:
        claude_tools = []
        for t in tools:
            fn = t.get("function", {})
            claude_tools.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system_msg,
        "messages": claude_messages,
        "stream": True,
    }
    if claude_tools:
        payload["tools"] = claude_tools

    for attempt in range(MAX_RETRIES):
        full_response = ""
        current_tool  = None

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                                         headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode()
                        friendly = parse_api_error(resp.status_code, body, "claude")
                        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAYS[attempt]
                            if on_chunk:
                                await on_chunk(f"\n⏳ **{resp.status_code}** — retrying in **{delay}s**…\n")
                            await asyncio.sleep(delay)
                            continue
                        if on_chunk:
                            await on_chunk(f"\n{friendly}\n")
                        return friendly

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        try:
                            data  = json.loads(line[6:])
                            etype = data.get("type", "")

                            if etype == "content_block_start":
                                block = data.get("content_block", {})
                                if block.get("type") == "tool_use":
                                    current_tool = {"id": block.get("id"), "name": block.get("name"), "input_str": ""}

                            elif etype == "content_block_delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    chunk = delta.get("text", "")
                                    full_response += chunk
                                    if on_chunk:
                                        await on_chunk(chunk)
                                elif delta.get("type") == "input_json_delta" and current_tool:
                                    current_tool["input_str"] += delta.get("partial_json", "")

                            elif etype == "content_block_stop" and current_tool:
                                try:
                                    args = json.loads(current_tool["input_str"]) if current_tool["input_str"] else {}
                                except Exception:
                                    args = {}
                                if on_tool_call:
                                    await on_tool_call(current_tool["id"], current_tool["name"], args)
                                current_tool = None

                        except Exception:
                            continue

        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                if on_chunk:
                    await on_chunk(f"\n⌛ Timed out — retrying in {delay}s…\n")
                await asyncio.sleep(delay)
                continue
            if on_chunk:
                await on_chunk("\n⌛ **Request timed out** after multiple attempts.\n")
            return ""

        return full_response

    return ""


# ─── Gemini streaming ─────────────────────────────────────────
async def stream_gemini(
    messages: list,
    api_key: str,
    model: str,
    tools: list = None,
    on_chunk: Callable = None,
    on_tool_call: Callable = None,
    json_mode: bool = False,
) -> str:
    contents = []
    system_text = SYSTEM_PROMPT
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
            continue
        if m["role"] == "tool":
            contents.append({"role": "user", "parts": [{"text": f"[Tool result]: {m.get('content', '')}"}]})
            continue
        role = "user" if m["role"] == "user" else "model"
        content = m.get("content", "")
        if isinstance(content, str):
            contents.append({"role": role, "parts": [{"text": content}]})
        elif isinstance(content, list):
            parts = [{"text": p["text"]} for p in content if isinstance(p, dict) and p.get("type") == "text"]
            contents.append({"role": role, "parts": parts})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_text}]},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    if tools:
        decls = []
        for t in tools:
            fn = t.get("function", {})
            decls.append({"name": fn.get("name"), "description": fn.get("description", ""), "parameters": fn.get("parameters", {})})
        payload["tools"] = [{"functionDeclarations": decls}]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={api_key}&alt=sse"

    for attempt in range(MAX_RETRIES):
        full_response = ""
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode()
                        friendly = parse_api_error(resp.status_code, body, "gemini")
                        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAYS[attempt]
                            if on_chunk:
                                await on_chunk(f"\n⏳ **{resp.status_code}** — retrying in **{delay}s**…\n")
                            await asyncio.sleep(delay)
                            continue
                        if on_chunk:
                            await on_chunk(f"\n{friendly}\n")
                        return friendly

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                            for cand in data.get("candidates", []):
                                for part in cand.get("content", {}).get("parts", []):
                                    if "text" in part:
                                        chunk = part["text"]
                                        full_response += chunk
                                        if on_chunk:
                                            await on_chunk(chunk)
                                    elif "functionCall" in part and on_tool_call:
                                        fc = part["functionCall"]
                                        await on_tool_call(
                                            f"gemini_{fc.get('name')}_{id(fc)}",
                                            fc.get("name"),
                                            fc.get("args", {}),
                                        )
                        except Exception:
                            continue

        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                if on_chunk:
                    await on_chunk(f"\n⌛ Timed out — retrying in {delay}s…\n")
                await asyncio.sleep(delay)
                continue
            if on_chunk:
                await on_chunk("\n⌛ **Request timed out** after multiple attempts.\n")
            return ""

        return full_response

    return ""


# ─── Unified entry point ──────────────────────────────────────
async def stream_response(
    messages: list,
    settings: dict,
    tools: list = None,
    on_chunk: Callable = None,
    on_tool_call: Callable = None,
    json_mode: bool = False,
) -> str:
    """Unified streaming interface for all providers."""
    # Enforce a 3-second cooldown between consecutive requests to avoid concurrency 429s
    await enforce_cooldown(3.0)

    provider = settings.get("active_provider", "openai")
    pconf    = settings.get("providers", {}).get(provider, {})

    # Validate API key (except local AI)
    if provider != "local":
        key = pconf.get("api_key", "").strip()
        if not key or key.startswith("*") or key.startswith("."):
            msg = (
                "**No API key configured.**\n\n"
                f"Go to **Settings** > **{provider.title()}** > enter your API key.\n\n"
                "For OpenRouter free models you still need an account key from openrouter.ai/keys"
            )
            if on_chunk:
                await on_chunk(msg)
            return msg

    try:
        if provider == "claude":
            return await stream_claude(
                messages=messages,
                api_key=pconf.get("api_key", ""),
                model=pconf.get("model", "claude-opus-4-5"),
                tools=tools, on_chunk=on_chunk, on_tool_call=on_tool_call,
                json_mode=json_mode,
            )

        elif provider == "gemini":
            return await stream_gemini(
                messages=messages,
                api_key=pconf.get("api_key", ""),
                model=pconf.get("model", "gemini-2.0-flash"),
                tools=tools, on_chunk=on_chunk, on_tool_call=on_tool_call,
                json_mode=json_mode,
            )

        elif provider in ("openai", "openrouter", "local"):
            extra = {}
            use_reasoning = False
            if provider == "openrouter":
                extra = {
                    "HTTP-Referer": "http://localhost:8000",
                    "X-Title": "AI Automation Chat",
                }
                # Enable reasoning for OpenRouter models that support it
                # (free tier models like gemma, deepseek, etc.)
                use_reasoning = pconf.get("reasoning", True)

            text, reasoning_details = await stream_openai_compatible(
                messages=messages,
                api_key=pconf.get("api_key", "ollama"),
                base_url=pconf.get("base_url", "https://api.openai.com/v1"),
                model=pconf.get("model", "gpt-4o"),
                tools=tools, on_chunk=on_chunk, on_tool_call=on_tool_call,
                extra_headers=extra,
                use_reasoning=use_reasoning,
                json_mode=json_mode,
            )
            # Attach reasoning_details to the last assistant entry so the
            # caller (main.py) can store it in history for multi-turn continuity
            if reasoning_details and on_tool_call is not None:
                # Signal reasoning_details to caller via a convention:
                # store on the function so main.py can retrieve it
                stream_response._last_reasoning_details = reasoning_details
            else:
                stream_response._last_reasoning_details = None
            return text

        else:
            msg = "❌ Unknown AI provider. Please select one in ⚙️ Settings."
            if on_chunk:
                await on_chunk(msg)
            return msg

    except Exception as e:
        msg = (
            f"\n**Unexpected error**: {str(e)}\n\n"
            "Please check your API key and network connection."
        )
        if on_chunk:
            await on_chunk(msg)
        return msg


def extract_json(text: str) -> dict:
    """Robustly extract JSON object from LLM response text."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        return {}


async def plan_task(
    messages: list,
    settings: dict,
    execution_log: list = None,
    page_state: dict = None,
) -> dict:
    """
    Call the AI planner to get the next step(s).
    Returns a dict with 'response', 'plan', and 'done'.
    """
    history = []
    for m in messages:
        if m.get("role") == "system":
            history.append({"role": "system", "content": PLANNER_SYSTEM_PROMPT})
        else:
            history.append(dict(m))
            
    if not any(m.get("role") == "system" for m in history):
        history.insert(0, {"role": "system", "content": PLANNER_SYSTEM_PROMPT})
        
    context_parts = []
    if execution_log:
        log_lines = []
        for i, item in enumerate(execution_log):
            step = item.get("step", {})
            res = item.get("result", {})
            success = res.get("success", True)
            err = res.get("error", "")
            blocked = res.get("blocked", False)
            status = "Success"
            if not success:
                status = f"Failed: {err}"
            elif blocked:
                status = "Blocked by anti-bot protection"
            log_lines.append(f"- Step {i+1}: {step.get('tool')}({step.get('args', {})}) -> {status}")
        context_parts.append("Execution History:\n" + "\n".join(log_lines))
        
    if page_state:
        context_parts.append(
            f"Current Page State:\n"
            f"- URL: {page_state.get('url', 'about:blank')}\n"
            f"- Title: {page_state.get('title', '')}\n"
            f"- Accessibility Snapshot:\n{page_state.get('snapshot', '(empty)')}"
        )
        
    if context_parts:
        context_msg = "\n\n".join(context_parts)
        history.append({"role": "user", "content": f"[System Update]\n{context_msg}\n\nProvide your next JSON plan."})

    response_chunks = []
    async def collect_chunks(chunk: str):
        response_chunks.append(chunk)
        
    await stream_response(
        messages=history,
        settings=settings,
        tools=None,
        on_chunk=collect_chunks,
        on_tool_call=None,
        json_mode=True,
    )
    
    full_text = "".join(response_chunks)
    parsed = extract_json(full_text)
    
    if not parsed and full_text.strip():
        return {
            "plan": [],
            "response": full_text.strip(),
            "done": True,
            "error": True,
        }

    if not isinstance(parsed, dict):
        parsed = {}
    if "plan" not in parsed or not isinstance(parsed["plan"], list):
        parsed["plan"] = []
    if "response" not in parsed:
        parsed["response"] = ""
    if "done" not in parsed:
        parsed["done"] = bool(parsed["response"] and not parsed["plan"])
        
    return parsed


async def summarize_execution(
    messages: list,
    settings: dict,
    execution_log: list,
    on_chunk: Callable,
) -> str:
    """
    Stream a natural language summary of the execution to the user.
    """
    history = []
    for m in messages:
        if m.get("role") == "system":
            history.append({"role": "system", "content": (
                "You are an AI assistant. Summarise the browser task execution results for the user. "
                "Output ONLY a clean, natural-language summary. Do not output JSON or code blocks unless requested. "
                "Be brief and friendly. Mention sites visited, actions taken, and the final result."
            )})
        else:
            history.append(dict(m))

    log_lines = []
    for i, item in enumerate(execution_log):
        step = item.get("step", {})
        res = item.get("result", {})
        success = res.get("success", True)
        err = res.get("error", "")
        log_lines.append(f"- Step {i+1}: {step.get('tool')}({step.get('args', {})}) -> {'Success' if success else 'Failed: ' + str(err)}")
        
    history.append({
        "role": "user",
        "content": (
            f"[System Update: Execution completed]\n"
            f"Here is the execution history:\n"
            f"{'\n'.join(log_lines)}\n\n"
            f"Please write the final summary for the user now."
        )
    })

    return await stream_response(
        messages=history,
        settings=settings,
        tools=None,
        on_chunk=on_chunk,
        on_tool_call=None,
        json_mode=False,
    )
