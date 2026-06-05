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
from app.ai_router import stream_response, SYSTEM_PROMPT, plan_task, summarize_execution
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

            execution_log = []
            max_rounds = 3
            round_count = 0
            replan_max = 2
            replan_count = 0
            task_done = False
            final_response = ""

            while round_count < max_rounds and not task_done:
                # Get current page state if browser is open
                page_state = None
                if bridge._browser and bridge._page and not bridge._page.is_closed():
                    try:
                        page_state = await bridge.get_accessibility_snapshot()
                    except Exception:
                        pass
                
                # Get the plan from AI planner
                try:
                    plan_data = await plan_task(
                        messages=history,
                        settings=settings,
                        execution_log=execution_log,
                        page_state=page_state,
                    )
                except Exception as pe:
                    await manager.send(websocket, {
                        "type": "chunk",
                        "content": f"❌ Error planning task: {str(pe)}"
                    })
                    break

                if plan_data.get("error"):
                    final_response = plan_data.get("response", "Unknown error during planning.")
                    task_done = True
                    break

                plan = plan_data.get("plan", [])
                response = plan_data.get("response", "")
                task_done = plan_data.get("done", False)

                # If the planner returned a direct response and no steps to execute, we are done
                if not plan:
                    if response:
                        final_response = response
                    task_done = True
                    break

                # Execute the planned steps
                plan_failed = False
                current_step_idx = 0
                while current_step_idx < len(plan):
                    step = plan[current_step_idx]
                    tool_name = step.get("tool")
                    args = step.get("args", {})

                    # Notify UI of tool starting
                    await manager.send(websocket, {
                        "type": "tool_call",
                        "tool": tool_name,
                        "args": args,
                    })

                    # Ensure browser is started before execution
                    try:
                        await ensure_browser()
                    except Exception as be:
                        err_detail = str(be)
                        result = {"success": False, "error": f"Browser failed to start: {err_detail}"}
                        await manager.send(websocket, {
                            "type": "chunk",
                            "content": f"\n\n**Browser could not start:**\n```\n{err_detail}\n```\nTry restarting the server.\n"
                        })
                        await manager.send(websocket, {"type": "tool_result", "tool": tool_name, "result": result})
                        execution_log.append({"step": step, "result": result})
                        plan_failed = True
                        break

                    # Execute the tool call
                    try:
                        result = await bridge.call_tool(tool_name, args)
                    except Exception as e:
                        result = {"success": False, "error": str(e)}

                    # Log execution result
                    execution_log.append({
                        "step": step,
                        "result": result,
                    })

                    # Notify UI of tool completion
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

                    # Check for step failure / anti-bot block
                    step_success = result.get("success", True)
                    step_blocked = result.get("blocked", False)
                    if not step_success or step_blocked:
                        plan_failed = True
                        break

                    current_step_idx += 1

                # If the plan failed, attempt to replan within limits
                if plan_failed:
                    if replan_count < replan_max:
                        replan_count += 1
                        round_count += 1
                        continue
                    else:
                        break

                round_count += 1

            # Done executing. Now generate the final summary response
            if final_response:
                await manager.send(websocket, {"type": "chunk", "content": final_response})
                history.append({"role": "assistant", "content": final_response})
            elif execution_log:
                response_parts = []
                async def on_summary_chunk(chunk: str):
                    response_parts.append(chunk)
                    await manager.send(websocket, {"type": "chunk", "content": chunk})

                await summarize_execution(
                    messages=history,
                    settings=settings,
                    execution_log=execution_log,
                    on_chunk=on_summary_chunk,
                )
                summary_text = "".join(response_parts)
                history.append({"role": "assistant", "content": summary_text})
            else:
                fallback_msg = "Task completed, but no details are available."
                await manager.send(websocket, {"type": "chunk", "content": fallback_msg})
                history.append({"role": "assistant", "content": fallback_msg})

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
