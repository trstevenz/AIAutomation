"""
server.py — Custom uvicorn launcher for Windows.

CRITICAL: WindowsProactorEventLoopPolicy MUST be set BEFORE uvicorn creates
its event loop. Running via `python server.py` ensures this.

Playwright requires ProactorEventLoop on Windows to spawn its Node.js subprocess.
SelectorEventLoop (uvicorn default on Windows) raises NotImplementedError for subprocesses.
"""
import sys
import asyncio

# ── Set ProactorEventLoop policy BEFORE any event loop is created ──────────
# This is the ONLY fix for: asyncio.base_events._make_subprocess_transport
#                            raise NotImplementedError
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn


def main():
    config = uvicorn.Config(
        app="app.main:app",
        host="0.0.0.0",
        port=8000,
        loop="none",        # Don't let uvicorn override our event loop policy
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    # asyncio.run() respects our ProactorEventLoop policy set above
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
