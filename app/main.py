"""
FastAPI main application — WebSocket chat, REST settings API, file serving.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.settings_manager import load_settings, save_settings
from app.ai_router import stream_response, SYSTEM_PROMPT
from app.mcp_bridge import get_bridge, BROWSER_TOOLS
from app.file_manager import list_downloads, list_screenshots, DOWNLOAD_DIR, SCREENSHOT_DIR, ensure_dirs

app = FastAPI(title="AI Automation Chat", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# File serving
ensure_dirs()
app.mount("/files/downloads", StaticFiles(directory=str(DOWNLOAD_DIR)), name="downloads")
app.mount("/files/screenshots", StaticFiles(directory=str(SCREENSHOT_DIR)), name="screenshots")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ──────────────────────────────────────────────
# SETTINGS API
# ──────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    settings = load_settings()
    # Mask API keys for the UI (show only last 4 chars)
    masked = json.loads(json.dumps(settings))
    for pname, pconf in masked.get("providers", {}).items():
        key = pconf.get("api_key", "")
        if key and len(key) > 4:
            masked["providers"][pname]["api_key"] = "•" * (len(key) - 4) + key[-4:]
    return JSONResponse(masked)


@app.put("/api/settings")
async def update_settings(request_body: dict):
    settings = load_settings()
    # Deep merge — preserve existing API keys if masked value sent
    for pname, pconf in request_body.get("providers", {}).items():
        new_key = pconf.get("api_key", "")
        if new_key and "•" in new_key:
            # User sent back the masked key — keep existing
            if pname in settings.get("providers", {}):
                request_body["providers"][pname]["api_key"] = settings["providers"][pname].get("api_key", "")

    from app.settings_manager import _deep_merge
    _deep_merge(settings, request_body)
    save_settings(settings)

    # Update browser headless setting if changed
    bridge = get_bridge()
    if bridge._browser:
        pw_conf = settings.get("playwright", {})
        # Can't change headless of running browser without restart; just update setting
        bridge._headless = pw_conf.get("headless", False)
        bridge._timeout = pw_conf.get("timeout", 30000)

    return JSONResponse({"status": "ok"})


# ──────────────────────────────────────────────
# FILES API
# ──────────────────────────────────────────────

@app.get("/api/files")
async def get_files():
    return JSONResponse({
        "downloads": list_downloads(),
        "screenshots": list_screenshots(),
    })


# ──────────────────────────────────────────────
# BROWSER STATUS API
# ──────────────────────────────────────────────

@app.get("/api/browser/status")
async def browser_status():
    bridge = get_bridge()
    running = bridge._browser is not None
    url = ""
    title = ""
    if running and bridge._page and not bridge._page.is_closed():
        try:
            url = bridge._page.url
            title = await bridge._page.title()
        except Exception:
            pass
    return JSONResponse({"running": running, "url": url, "title": title})


@app.post("/api/browser/stop")
async def browser_stop():
    bridge = get_bridge()
    await bridge.stop()
    return JSONResponse({"status": "stopped"})


@app.post("/api/browser/start")
async def browser_start():
    settings = load_settings()
    pw_conf = settings.get("playwright", {})
    bridge = get_bridge()
    if not bridge._browser:
        try:
            await bridge.start(
                headless=pw_conf.get("headless", False),
                timeout=pw_conf.get("timeout", 30000),
            )
        except Exception as e:
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
    return JSONResponse({"status": "started"})


# ──────────────────────────────────────────────
# WEBSOCKET CHAT
# ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def send(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            pass


manager = ConnectionManager()

# Per-connection chat history
chat_histories: dict[str, list] = {}


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    await manager.connect(websocket)
    conn_id = str(id(websocket))

    # Initialize history with system prompt
    chat_histories[conn_id] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]

    settings = load_settings()
    bridge = get_bridge()

    # ── Do NOT auto-start browser here — start lazily on first tool call ──
    # Send a ready signal so the UI shows MCP as available immediately
    await manager.send(websocket, {"type": "mcp_ready", "tools": 20})

    # Helper: ensure browser is running before any tool call
    async def ensure_browser():
        if not bridge._browser:
            # Re-read settings fresh in case user changed headless toggle
            cur_settings = load_settings()
            pw_conf = cur_settings.get("playwright", {})
            headless = pw_conf.get("headless", True)   # default headless
            timeout  = pw_conf.get("timeout", 30000)
            await manager.send(websocket, {
                "type": "tool_status",
                "tool": "browser",
                "content": f"Starting Playwright Chromium ({'headless' if headless else 'visible'} mode)...",
            })
            await bridge.start(headless=headless, timeout=timeout)

    # Set up bridge event callbacks
    async def on_browser_event(event_type: str, data: dict):
        """
        Route browser events to the SIDEBAR (activity log / browser panel) only.
        NEVER inject tool progress into the main chat message stream — that would be messy.
        The AI's final natural-language reply in the chat is the only user-facing output.
        """
        if event_type == "tool_start":
            tool   = data.get("tool", "")
            detail = data.get("url") or data.get("selector") or ""
            # → sidebar activity log only
            await manager.send(websocket, {
                "type": "tool_call",
                "tool": tool,
                "args": {"detail": detail},
            })

        elif event_type == "tool_result":
            result  = data.get("result", {})
            tool    = data.get("tool", "")
            success = result.get("success", True)
            detail  = (result.get("url") or result.get("title") or
                       result.get("value") or ("OK" if success else result.get("error", "Error")))
            # → sidebar activity log only
            await manager.send(websocket, {
                "type": "tool_result",
                "tool": tool,
                "result": {
                    "success": success,
                    "detail": str(detail)[:120],
                },
            })
            # Screenshots: update the browser panel, NOT injected into chat
            if "screenshot" in result and tool == "screenshot":
                await manager.send(websocket, {
                    "type": "screenshot",
                    "url": result["screenshot"],
                })

        elif event_type == "live_preview":
            # Live browser state → right-panel preview (never injected into chat)
            await manager.send(websocket, {
                "type":       "live_preview",
                "url":        data.get("url", ""),
                "title":      data.get("title", ""),
                "screenshot": data.get("screenshot", ""),
            })

    bridge.set_event_callback(on_browser_event)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "chat")

            if msg_type == "settings_update":
                # Reload settings on settings update notification
                settings = load_settings()
                await manager.send(websocket, {"type": "settings_ack"})
                continue

            if msg_type == "clear_history":
                chat_histories[conn_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
                await manager.send(websocket, {"type": "history_cleared"})
                continue

            user_content = msg.get("content", "").strip()
            if not user_content:
                continue

            # Add user message to history
            history = chat_histories[conn_id]
            history.append({"role": "user", "content": user_content})

            # Signal start of AI response
            await manager.send(websocket, {"type": "start"})

            # Reload settings fresh for each message (in case user updated)
            settings = load_settings()

            ai_response_parts = []
            tool_results_this_turn = []
            max_tool_rounds = 15

            async def on_chunk(chunk: str):
                ai_response_parts.append(chunk)
                await manager.send(websocket, {"type": "chunk", "content": chunk})

            async def on_tool_call(tool_id: str, tool_name: str, args: dict):
                # Notify UI
                await manager.send(websocket, {
                    "type": "tool_call",
                    "tool": tool_name,
                    "args": args,
                })

                # Ensure browser is started before any tool call
                try:
                    await ensure_browser()
                except Exception as be:
                    err_detail = str(be)
                    result = {"success": False, "error": f"Browser failed to start: {err_detail}"}
                    await manager.send(websocket, {
                        "type": "chunk",
                        "content": f"\n\n**Browser could not start:**\n```\n{err_detail}\n```\nTry clicking **▶ Start** in the Browser panel, or restart the server.\n"
                    })
                    await manager.send(websocket, {"type": "tool_result", "tool": tool_name, "result": result})
                    tool_results_this_turn.append({"tool_call_id": tool_id, "tool_name": tool_name, "result": result})
                    return

                # Execute tool
                try:
                    result = await bridge.call_tool(tool_name, args)
                except Exception as e:
                    result = {"success": False, "error": str(e)}

                tool_results_this_turn.append({
                    "tool_call_id": tool_id,
                    "tool_name": tool_name,
                    "result": result,
                })

                # Send tool result to UI
                await manager.send(websocket, {
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": result,
                })

                # Send PDF notification if PDF was created/downloaded
                if "url" in result and (result.get("url", "").endswith(".pdf") or "downloads" in result.get("url", "")):
                    await manager.send(websocket, {
                        "type": "pdf_ready",
                        "url": result["url"],
                        "filename": result.get("filename", "document.pdf"),
                    })

            # Agentic loop: run AI -> tool calls -> AI again
            for _round in range(max_tool_rounds):
                tool_results_this_turn = []
                ai_response_parts = []

                await stream_response(
                    messages=history,
                    settings=settings,
                    tools=BROWSER_TOOLS,
                    on_chunk=on_chunk,
                    on_tool_call=on_tool_call,
                )

                assistant_text = "".join(ai_response_parts)

                # Build assistant message for history
                if tool_results_this_turn:
                    asst_msg = {"role": "assistant", "content": assistant_text or " "}
                    # Preserve reasoning_details for OpenRouter multi-turn continuity
                    rd = getattr(stream_response, '_last_reasoning_details', None)
                    if rd:
                        asst_msg["reasoning_details"] = rd
                    history.append(asst_msg)
                    for tr in tool_results_this_turn:
                        history.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_call_id"],
                            "content": json.dumps(tr["result"]),
                        })
                    # Continue the loop so AI can process tool results
                    continue
                else:
                    # No tool calls — AI is done
                    asst_msg = {"role": "assistant", "content": assistant_text}
                    rd = getattr(stream_response, '_last_reasoning_details', None)
                    if rd:
                        asst_msg["reasoning_details"] = rd
                    history.append(asst_msg)
                    break

            await manager.send(websocket, {"type": "end"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if conn_id in chat_histories:
            del chat_histories[conn_id]
    except Exception as e:
        await manager.send(websocket, {"type": "error", "content": f"❌ Error: {str(e)}"})
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
