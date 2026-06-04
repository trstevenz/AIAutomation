import os
import shutil
from pathlib import Path
from datetime import datetime

DOWNLOAD_DIR = Path("data/downloads")
SCREENSHOT_DIR = Path("data/screenshots")


def ensure_dirs():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def save_screenshot(image_bytes: bytes, prefix: str = "screenshot", ext: str = "png") -> str:
    ensure_dirs()
    # For live previews, always overwrite the same file so disk doesn't fill up
    if prefix == "live":
        path = SCREENSHOT_DIR / f"live.{ext}"
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
        path = SCREENSHOT_DIR / f"{prefix}_{ts}.{ext}"
    with open(path, "wb") as f:
        f.write(image_bytes)
    return str(path)


def save_pdf(pdf_bytes: bytes, filename: str = None) -> str:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not filename:
        filename = f"document_{ts}.pdf"
    path = DOWNLOAD_DIR / filename
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return str(path)


def list_downloads() -> list[dict]:
    ensure_dirs()
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime
            })
    return files


def list_screenshots() -> list[dict]:
    ensure_dirs()
    files = []
    for f in sorted(SCREENSHOT_DIR.iterdir(), reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime
            })
    return files


def get_file_url(path: str) -> str:
    """Convert local path to a URL served by FastAPI."""
    p = Path(path)
    if "screenshots" in path:
        return f"/files/screenshots/{p.name}"
    return f"/files/downloads/{p.name}"
