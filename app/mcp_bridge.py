"""
Playwright MCP Bridge — manages the Playwright MCP server subprocess and exposes
browser automation tools that the AI can call.

Uses Python's playwright library directly (no Node dependency) with a tool registry
that mirrors the MCP tool interface so the AI router can call them seamlessly.
"""

import asyncio
import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.file_manager import save_screenshot, save_pdf, get_file_url
import time as _time_module

# Try to import playwright; degrade gracefully if not installed yet
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _get_system_proxy() -> dict | None:
    """
    Read Windows Internet Settings proxy and return Playwright proxy dict, or None.
    Also checks the HTTP_PROXY / HTTPS_PROXY environment variables.
    """
    import os

    # 1. Check env vars first (works on any OS, also set by corporate tools)
    for env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(env, "").strip()
        if val:
            return {"server": val}

    # 2. Read Windows registry Internet Settings
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if proxy_enable:
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if proxy_server:
                if "://" not in proxy_server:
                    proxy_server = "http://" + proxy_server
                return {"server": proxy_server}
    except Exception:
        pass

    return None  # no proxy found — connect directly


class PlaywrightBridge:
    """Manages a Playwright browser instance and provides MCP-style tool calls."""

    def __init__(self):
        self._playwright  = None
        self._browser     = None
        self._context     = None
        self._page        = None
        self._headless    = False   # always visible — live preview in UI
        self._timeout     = 30000
        self._on_event    = None
        self._starting    = False
        self._start_error = ""

    async def start(self, headless: bool = False, timeout: int = 30000):
        """Start Playwright Chromium in VISIBLE mode so user can see what's happening."""
        import traceback as _tb

        if self._browser is not None:
            return  # already running

        if self._starting:
            # Another coroutine is already starting — wait for it
            for _ in range(100):
                await asyncio.sleep(0.1)
                if self._browser is not None:
                    return
                if not self._starting:
                    break
            return

        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright not installed. "
                "Run: venv\\Scripts\\python.exe -m playwright install chromium"
            )

        self._starting    = True
        self._headless    = headless
        self._timeout     = timeout
        self._start_error = ""

        try:
            # async_playwright().start() is the correct, loop-safe way
            from playwright.async_api import async_playwright as _apw
            self._playwright = await _apw().start()

            proxy = _get_system_proxy()
            launch_args = {
                "headless": headless,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-blink-features=AutomationControlled",  # key stealth flag
                    "--disable-infobars",
                    "--window-size=1280,900",
                ],
            }
            if proxy:
                launch_args["proxy"] = proxy

            self._browser = await self._playwright.chromium.launch(**launch_args)

            # Stealth context: realistic fingerprinting to avoid bot-detection
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                permissions=["geolocation"],
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Override automation-detection fingerprints on every page
            await self._context.add_init_script("""
                // Hide webdriver flag
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

                // Add a realistic chrome object
                window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };

                // Fake realistic plugin list
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ]
                });

                // Realistic language
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

                // Permissions API — return granted for notifications
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (params) =>
                    params.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(params);
            """)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(timeout)

        except Exception as exc:
            full_tb = _tb.format_exc()
            self._start_error = full_tb
            # Clean up partial state
            try:
                if self._browser:
                    await self._browser.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    await self._playwright.stop()
            except Exception:
                pass
            self._browser    = None
            self._context    = None
            self._page       = None
            self._playwright = None
            raise RuntimeError(full_tb) from exc

        finally:
            self._starting = False

    async def stop(self):
        """Close the browser cleanly."""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser    = None
        self._context    = None
        self._page       = None
        self._playwright = None

    def set_event_callback(self, callback):
        self._on_event = callback

    async def _emit(self, event_type: str, data: dict):
        if self._on_event:
            try:
                await self._on_event(event_type, data)
            except Exception:
                pass

    async def ensure_page(self):
        """Make sure browser + page are available; start if needed."""
        if self._browser is None:
            await self.start(False, self._timeout)   # always visible
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self._timeout)

    # ────────────────────────────────────────────────
    # LIVE PREVIEW — auto-screenshot after every page action
    # Sends to the right-panel browser preview, NOT injected into chat.
    # ────────────────────────────────────────────────
    async def _live_preview(self):
        """
        Fast live preview: JPEG thumbnail, debounced to max 1 per 400ms.
        Much faster than full PNG — JPEG is 5-10x smaller, encodes faster.
        """
        now = _time_module.monotonic()
        last = getattr(self, '_last_preview_time', 0)
        if now - last < 0.4:   # debounce: skip if last preview was <400ms ago
            return
        self._last_preview_time = now

        try:
            if self._page and not self._page.is_closed():
                # JPEG at 70% quality — ~10x smaller than PNG, fast to encode + transfer
                ss_bytes = await self._page.screenshot(
                    full_page=False,
                    timeout=4000,
                    type="jpeg",
                    quality=70,
                    clip={"x": 0, "y": 0, "width": 1280, "height": 800}
                )
                ss_path = save_screenshot(ss_bytes, "live", ext="jpg")
                ss_url  = get_file_url(ss_path)
                await self._emit("live_preview", {
                    "url":        self._page.url,
                    "title":      await self._page.title(),
                    "screenshot": ss_url,
                })
        except Exception:
            pass   # preview is always best-effort

    # ────────────────────────────────────────────────
    # BLOCKING DETECTION
    # ────────────────────────────────────────────────
    _BLOCK_SIGNALS = [
        "access denied", "403 forbidden", "just a moment", "captcha",
        "cloudflare", "bot detected", "automated access", "unusual traffic",
        "verify you are human", "security check", "ddos protection",
        "please verify", "are you a robot", "rate limited", "too many requests",
        "enable javascript", "blocked", "robot or human",
    ]

    async def _check_blocked(self) -> tuple[bool, str]:
        """Return (is_blocked, reason_string)."""
        try:
            title = (await self._page.title()).lower()
            for sig in self._BLOCK_SIGNALS:
                if sig in title:
                    return True, await self._page.title()
            # Check first 800 chars of body text
            try:
                body = (await self._page.inner_text("body", timeout=2000)).lower()[:800]
                for sig in self._BLOCK_SIGNALS:
                    if sig in body:
                        return True, sig.title()
            except Exception:
                pass
        except Exception:
            pass
        return False, ""

    # ────────────────────────────────────────────────
    # TOOL: navigate  (with blocking detection + live preview)
    # ────────────────────────────────────────────────
    async def navigate(self, url: str) -> dict:
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "navigate", "url": url})

        if not url.startswith(("http://", "https://", "file://", "about:")):
            url = "https://" + url

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as nav_err:
            err_str = str(nav_err)
            if url.startswith("https://") and any(e in err_str for e in ("ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_CERT")):
                try:
                    await self._page.goto(url.replace("https://", "http://", 1), wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    return {"success": False, "error": f"Could not reach {url}. The site may be down or unreachable."}
            else:
                return {"success": False, "error": f"Navigation failed: {err_str}"}

        title       = await self._page.title()
        current_url = self._page.url
        # NO automatic screenshot — screenshots only when user explicitly asks
        result = {"success": True, "url": current_url, "title": title}
        await self._emit("tool_result", {"tool": "navigate", "result": result})
        return result

    # ────────────────────────────────────────────────
    # SMART ELEMENT FINDER
    # Tries multiple Playwright locator strategies in order until one works.
    # ────────────────────────────────────────────────
    async def _smart_find(self, hint: str, role_hint: str = None):
        """
        Try to find an element using multiple strategies:
        1. Exact CSS/XPath selector (if hint looks like one)
        2. By role + accessible name (button, link, textbox...)
        3. By visible text
        4. By aria-label
        5. By placeholder
        6. By label text
        Returns the first matching Playwright Locator, or raises if nothing found.
        """
        page = self._page
        TIMEOUT = 5000

        # 1 ─ CSS/XPath selector
        if hint and (hint.startswith(("#", ".", "[", "//", "xpath=")) or " " not in hint.strip()):
            try:
                loc = page.locator(hint).first
                await loc.wait_for(state="visible", timeout=TIMEOUT)
                return loc
            except Exception:
                pass  # fall through to other strategies

        # 2 ─ Role-based (handles buttons, links, inputs by accessible name)
        if role_hint:
            for role in [role_hint]:
                try:
                    loc = page.get_by_role(role, name=hint)
                    await loc.first.wait_for(state="visible", timeout=TIMEOUT)
                    return loc.first
                except Exception:
                    pass
        # Auto-detect role
        for role in ("button", "link", "textbox", "combobox", "searchbox", "checkbox", "radio"):
            try:
                loc = page.get_by_role(role, name=hint)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # 3 ─ Visible text
        try:
            loc = page.get_by_text(hint, exact=False)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 4 ─ Aria-label
        try:
            loc = page.locator(f'[aria-label*="{hint}" i]')
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 5 ─ Placeholder
        try:
            loc = page.get_by_placeholder(hint, exact=False)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 6 ─ Label text
        try:
            loc = page.get_by_label(hint, exact=False)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        # 7 ─ Title attribute
        try:
            loc = page.get_by_title(hint, exact=False)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

        raise ValueError(f"Could not find element matching: {hint!r}")

    # ────────────────────────────────────────────────
    # TOOL: screenshot  (ONLY tool that sends screenshot to chat)
    # ────────────────────────────────────────────────
    async def screenshot(self, full_page: bool = False) -> dict:
        await self.ensure_page()
        ss_bytes = await self._page.screenshot(full_page=full_page)
        ss_path  = save_screenshot(ss_bytes, "screenshot")
        ss_url   = get_file_url(ss_path)
        result   = {"success": True, "screenshot": ss_url, "path": ss_path}
        await self._emit("tool_result", {"tool": "screenshot", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: click  (smart multi-strategy)
    # ────────────────────────────────────────────────
    async def click(self, selector: str = None, text: str = None, x: int = None, y: int = None) -> dict:
        await self.ensure_page()
        hint = selector or text
        await self._emit("tool_start", {"tool": "click", "selector": hint})

        try:
            if x is not None and y is not None:
                await self._page.mouse.click(x, y)
            elif hint:
                loc = await self._smart_find(hint)
                await loc.click(timeout=10000)
            else:
                return {"success": False, "error": "Provide selector, text, or x/y coordinates"}

            # Wait briefly for any navigation/AJAX triggered by click
            try:
                await self._page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            result = {"success": True, "clicked": hint or f"({x},{y})", "url": self._page.url}
        except Exception as e:
            result = {"success": False, "error": str(e)}

        await self._live_preview()   # update right panel after click
        await self._emit("tool_result", {"tool": "click", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: fill  (smart label/placeholder/role finding)
    # ────────────────────────────────────────────────
    async def fill(self, selector: str, value: str) -> dict:
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "fill", "selector": selector})

        try:
            loc = await self._smart_find(selector, role_hint="textbox")
            await loc.fill(value, timeout=10000)
            result = {"success": True, "field": selector, "value": value}
        except Exception as e:
            # Last resort: direct fill by CSS selector
            try:
                await self._page.fill(selector, value, timeout=5000)
                result = {"success": True, "field": selector, "value": value}
            except Exception as e2:
                result = {"success": False, "error": f"Could not fill '{selector}': {e2}"}

        await self._live_preview()   # update right panel after fill
        await self._emit("tool_result", {"tool": "fill", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: select_option
    # ────────────────────────────────────────────────
    async def select_option(self, selector: str, value: str) -> dict:
        await self.ensure_page()
        await self._page.select_option(selector, value)
        result = {"success": True, "selector": selector, "value": value}
        await self._emit("tool_result", {"tool": "select_option", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: type_text
    # ────────────────────────────────────────────────
    async def type_text(self, selector: str, text: str) -> dict:
        await self.ensure_page()
        await self._page.click(selector)
        await self._page.keyboard.type(text)
        result = {"success": True}
        await self._emit("tool_result", {"tool": "type_text", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: get_text
    # ────────────────────────────────────────────────
    async def get_text(self, selector: str = "body") -> dict:
        await self.ensure_page()
        try:
            text = await self._page.inner_text(selector)
        except Exception:
            text = await self._page.content()
        result = {"success": True, "text": text[:8000]}  # cap to avoid token overflow
        await self._emit("tool_result", {"tool": "get_text", "result": {"length": len(text)}})
        return result

    # ────────────────────────────────────────────────
    # TOOL: get_html
    # ────────────────────────────────────────────────
    async def get_html(self, selector: str = "body") -> dict:
        await self.ensure_page()
        html = await self._page.inner_html(selector)
        result = {"success": True, "html": html[:10000]}
        return result

    # ────────────────────────────────────────────────
    # TOOL: find_links
    # ────────────────────────────────────────────────
    async def find_links(self, pattern: str = "") -> dict:
        await self.ensure_page()
        links = await self._page.eval_on_selector_all(
            "a[href]",
            """(els, pattern) => els
                .map(e => ({text: e.innerText.trim(), href: e.href}))
                .filter(l => !pattern || l.href.includes(pattern) || l.text.toLowerCase().includes(pattern.toLowerCase()))
                .slice(0, 50)""",
            pattern,
        )
        result = {"success": True, "links": links}
        await self._emit("tool_result", {"tool": "find_links", "result": {"count": len(links)}})
        return result

    # ────────────────────────────────────────────────
    # TOOL: find_pdfs
    # ────────────────────────────────────────────────
    async def find_pdfs(self) -> dict:
        await self.ensure_page()
        links = await self._page.eval_on_selector_all(
            "a[href]",
            """(els) => els
                .filter(e => e.href.toLowerCase().endsWith('.pdf') || e.href.toLowerCase().includes('pdf'))
                .map(e => ({text: e.innerText.trim() || 'PDF', href: e.href}))
                .slice(0, 20)"""
        )
        result = {"success": True, "pdfs": links}
        await self._emit("tool_result", {"tool": "find_pdfs", "result": {"count": len(links)}})
        return result

    # ────────────────────────────────────────────────
    # TOOL: download_pdf
    # ────────────────────────────────────────────────
    async def download_pdf(self, url: str, filename: str = None) -> dict:
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "download_pdf", "url": url})
        import httpx as hx
        try:
            async with hx.AsyncClient(timeout=60) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content = resp.content
                if not filename:
                    cd = resp.headers.get("content-disposition", "")
                    m = re.search(r'filename="?([^";]+)"?', cd)
                    filename = m.group(1) if m else url.split("/")[-1].split("?")[0]
                    if not filename.endswith(".pdf"):
                        filename += ".pdf"
                path = save_pdf(content, filename)
                url_path = get_file_url(path)
                result = {"success": True, "path": path, "url": url_path, "filename": filename, "size": len(content)}
        except Exception as e:
            result = {"success": False, "error": str(e)}
        await self._emit("tool_result", {"tool": "download_pdf", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: print_to_pdf
    # ────────────────────────────────────────────────
    async def print_to_pdf(self, filename: str = None) -> dict:
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "print_to_pdf"})
        pdf_bytes = await self._page.pdf(format="A4", print_background=True)
        if not filename:
            title = await self._page.title()
            safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', title)[:30]
            filename = f"{safe_title}.pdf"
        path = save_pdf(pdf_bytes, filename)
        url_path = get_file_url(path)
        result = {"success": True, "path": path, "url": url_path, "filename": filename}
        await self._emit("tool_result", {"tool": "print_to_pdf", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: wait_for_selector
    # ────────────────────────────────────────────────
    async def wait_for_selector(self, selector: str, timeout: int = 10000) -> dict:
        await self.ensure_page()
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            result = {"success": True, "selector": selector}
        except Exception as e:
            result = {"success": False, "error": str(e)}
        return result

    # ────────────────────────────────────────────────
    # TOOL: execute_js
    # ────────────────────────────────────────────────
    async def execute_js(self, script: str) -> dict:
        await self.ensure_page()
        try:
            value = await self._page.evaluate(script)
            result = {"success": True, "value": str(value)}
        except Exception as e:
            result = {"success": False, "error": str(e)}
        return result

    # ────────────────────────────────────────────────
    # TOOL: get_form_fields
    # ────────────────────────────────────────────────
    async def get_form_fields(self) -> dict:
        await self.ensure_page()
        fields = await self._page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
            return inputs.map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                value: el.value || '',
                required: el.required,
                selector: el.id ? '#' + el.id : (el.name ? '[name="' + el.name + '"]' : el.tagName.toLowerCase())
            })).slice(0, 50);
        }""")
        result = {"success": True, "fields": fields}
        await self._emit("tool_result", {"tool": "get_form_fields", "result": {"count": len(fields)}})
        return result

    # ────────────────────────────────────────────────
    # TOOL: submit_form
    # ────────────────────────────────────────────────
    async def submit_form(self, selector: str = "form") -> dict:
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "submit_form"})
        try:
            await self._page.evaluate(f"""() => {{
                const form = document.querySelector('{selector}');
                if (form) form.submit();
            }}""")
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            # Try pressing Enter instead
            await self._page.keyboard.press("Enter")
        ss_bytes = await self._page.screenshot(full_page=True)
        ss_path = save_screenshot(ss_bytes, "after_submit")
        ss_url = get_file_url(ss_path)
        page_text = await self._page.inner_text("body")

        # Try to extract confirmation/reference numbers
        numbers = re.findall(r'(?:reference|confirmation|form|ticket|no\.?|number|id)[:\s#]*([A-Z0-9\-]{4,20})', page_text, re.IGNORECASE)

        result = {
            "success": True,
            "screenshot": ss_url,
            "url": self._page.url,
            "confirmation_numbers": list(set(numbers))[:10],
            "page_text_preview": page_text[:2000],
        }
        await self._emit("tool_result", {"tool": "submit_form", "result": result})
        return result

    # ────────────────────────────────────────────────
    # TOOL: scroll
    # ────────────────────────────────────────────────
    async def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        await self.ensure_page()
        dy = amount if direction == "down" else -amount
        await self._page.mouse.wheel(0, dy)
        result = {"success": True}
        return result

    # ────────────────────────────────────────────────
    # TOOL: go_back
    # ────────────────────────────────────────────────
    async def go_back(self) -> dict:
        await self.ensure_page()
        await self._page.go_back()
        result = {"success": True, "url": self._page.url}
        return result

    # ────────────────────────────────────────────────
    # TOOL: get_page_info
    # ────────────────────────────────────────────────
    async def get_page_info(self) -> dict:
        await self.ensure_page()
        return {
            "success": True,
            "url": self._page.url,
            "title": await self._page.title(),
        }

    # ────────────────────────────────────────────────
    # TOOL: get_accessibility_snapshot
    # Token-efficient page understanding — replaces get_html/get_text
    # Returns compact accessibility tree: ~200-400 tokens vs thousands for HTML
    # ────────────────────────────────────────────────
    async def get_accessibility_snapshot(self, interesting_only: bool = True) -> dict:
        """
        Returns a compact, token-efficient accessibility tree of the page.
        Much cheaper than get_html (~200-400 tokens vs thousands).
        Use this to understand what's on the page before clicking.
        """
        await self.ensure_page()
        await self._emit("tool_start", {"tool": "get_accessibility_snapshot", "selector": "page"})

        try:
            # Playwright's built-in accessibility snapshot
            raw = await self._page.accessibility.snapshot(interesting_only=interesting_only)

            if not raw:
                result = {"success": True, "snapshot": "(empty page or no accessible elements)",
                          "url": self._page.url, "title": await self._page.title()}
                await self._emit("tool_result", {"tool": "get_accessibility_snapshot", "result": result})
                return result

            # Flatten the tree into a compact readable format
            def _flatten(node, depth=0, lines=None):
                if lines is None:
                    lines = []
                indent = "  " * depth
                role  = node.get("role", "")
                name  = node.get("name", "")
                value = node.get("value", "")
                desc  = node.get("description", "")

                # Skip invisible / structural noise
                if role in ("none", "generic", "group", "region", "img") and not name:
                    for child in node.get("children", []):
                        _flatten(child, depth, lines)
                    return lines

                parts = [f"{indent}[{role}]"]
                if name:  parts.append(f" \"{name}\"")
                if value: parts.append(f" = {value!r}")
                if desc and desc != name: parts.append(f" ({desc[:60]})")
                lines.append("".join(parts))

                for child in node.get("children", []):
                    _flatten(child, depth + 1, lines)
                return lines

            lines  = _flatten(raw)
            # Cap at 120 lines to avoid token explosion on huge pages
            if len(lines) > 120:
                lines = lines[:120] + [f"... ({len(lines) - 120} more elements truncated)"]

            snapshot_text = "\n".join(lines)
            result = {
                "success": True,
                "url":      self._page.url,
                "title":    await self._page.title(),
                "snapshot": snapshot_text,
                "elements": len(lines),
            }
        except Exception as e:
            result = {"success": False, "error": f"Accessibility snapshot failed: {e}"}

        await self._emit("tool_result", {"tool": "get_accessibility_snapshot", "result": result})
        return result

    async def call_tool(self, tool_name: str, args: dict) -> dict:
        """Dispatch a tool call by name."""
        tool_map = {
            "navigate": lambda: self.navigate(args.get("url", "")),
            "screenshot": lambda: self.screenshot(args.get("full_page", False)),
            "click": lambda: self.click(
                selector=args.get("selector"),
                text=args.get("text"),
                x=args.get("x"),
                y=args.get("y"),
            ),
            "fill": lambda: self.fill(args.get("selector", ""), args.get("value", "")),
            "select_option": lambda: self.select_option(args.get("selector", ""), args.get("value", "")),
            "type_text": lambda: self.type_text(args.get("selector", ""), args.get("text", "")),
            "get_text": lambda: self.get_text(args.get("selector", "body")),
            "get_html": lambda: self.get_html(args.get("selector", "body")),
            "find_links": lambda: self.find_links(args.get("pattern", "")),
            "find_pdfs": lambda: self.find_pdfs(),
            "download_pdf": lambda: self.download_pdf(args.get("url", ""), args.get("filename")),
            "print_to_pdf": lambda: self.print_to_pdf(args.get("filename")),
            "wait_for_selector": lambda: self.wait_for_selector(args.get("selector", ""), args.get("timeout", 10000)),
            "execute_js": lambda: self.execute_js(args.get("script", "")),
            "get_form_fields": lambda: self.get_form_fields(),
            "submit_form": lambda: self.submit_form(args.get("selector", "form")),
            "scroll": lambda: self.scroll(args.get("direction", "down"), args.get("amount", 500)),
            "go_back": lambda: self.go_back(),
            "get_page_info": lambda: self.get_page_info(),
            "get_accessibility_snapshot": lambda: self.get_accessibility_snapshot(
                args.get("interesting_only", True)
            ),
        }
        fn = tool_map.get(tool_name)
        if fn:
            return await fn()
        return {"success": False, "error": f"Unknown tool: {tool_name}"}


# ─────────────────────────────────────────────────────────────
# MCP TOOL SCHEMAS (OpenAI function-calling format)
# ─────────────────────────────────────────────────────────────
BROWSER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL. Returns the page title, current URL, and a screenshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to navigate to"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot of the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "Whether to capture the full scrollable page"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click on an element on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the element to click"},
                    "text": {"type": "string", "description": "Visible text of the element to click"},
                    "x": {"type": "integer", "description": "X coordinate for click"},
                    "y": {"type": "integer", "description": "Y coordinate for click"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Fill an input field with a value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the input field"},
                    "value": {"type": "string", "description": "Value to fill"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option from a dropdown/select element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the select element"},
                    "value": {"type": "string", "description": "The value or label to select"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into a focused element character by character (for special inputs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to focus"},
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_text",
            "description": "Get the visible text content of a page or element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector (default: body for full page)"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_links",
            "description": "Find all links on the current page, optionally filtered by a pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Optional filter pattern (text or URL substring)"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_pdfs",
            "description": "Find all PDF links on the current page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_pdf",
            "description": "Download a PDF from a URL and save it locally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the PDF to download"},
                    "filename": {"type": "string", "description": "Optional filename to save as"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "print_to_pdf",
            "description": "Print the current page to a PDF file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Optional filename for the PDF"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_form_fields",
            "description": "Get all form fields on the current page with their selectors and current values.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_form",
            "description": "Submit a form on the current page and wait for the result. Returns confirmation numbers if found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the form (default: 'form')"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_selector",
            "description": "Wait for an element to appear on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to wait for"},
                    "timeout": {"type": "integer", "description": "Timeout in milliseconds (default 10000)"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_js",
            "description": "Execute JavaScript code on the current page and return the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JavaScript code to execute"}
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page up or down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                    "amount": {"type": "integer", "description": "Pixels to scroll (default 500)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Navigate back to the previous page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page_info",
            "description": "Get the current page URL and title.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_accessibility_snapshot",
            "description": (
                "PREFERRED over get_text/get_html for understanding page structure. "
                "Returns a compact accessibility tree of the page: roles, labels, "
                "interactive elements (buttons, inputs, links, headings). "
                "Very token-efficient (~200-400 tokens vs thousands for full HTML). "
                "Use this to understand form fields, buttons, and navigation before clicking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "interesting_only": {
                        "type": "boolean",
                        "description": "If true (default), only show interactive and heading elements"
                    }
                },
            },
        },
    },
]

# Global browser bridge instance
_bridge: PlaywrightBridge = None


def get_bridge() -> PlaywrightBridge:
    global _bridge
    if _bridge is None:
        _bridge = PlaywrightBridge()
    return _bridge
