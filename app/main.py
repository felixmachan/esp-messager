import io
import json
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps

APP_TITLE = "ESP-MESSAGER"
API_TOKEN = os.getenv("API_TOKEN", "RANDOM123")
STATE_PATH = Path(os.getenv("STATE_PATH", "/app/data/state.json"))
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "")
TENOR_FEATURE_ENABLED = os.getenv("TENOR_FEATURE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
MAX_SOURCE_GIF_BYTES = int(os.getenv("MAX_SOURCE_GIF_BYTES", "15000000"))
MAX_FRAMES = int(os.getenv("MAX_FRAMES", "40"))

FRAME_WIDTH = 128
FRAME_HEIGHT = 160
FRAME_ROTATE_DEG = 0
FRAME_PIPELINE_VERSION = os.getenv("FRAME_PIPELINE_VERSION", "v6")
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 2

DATA_DIR = STATE_PATH.parent
GIF_DIR = DATA_DIR / "gifs"

DEFAULT_STATE = {
    "type": "text",
    "text": "",
    "gif_url": "",
    "gif_id": "",
    "frame_count": 0,
    "frame_delay_ms": 120,
    "frame_width": FRAME_WIDTH,
    "frame_height": FRAME_HEIGHT,
    "frame_pipeline_version": FRAME_PIPELINE_VERSION,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("esp-messager")

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory="app/templates")
_state_lock = threading.Lock()


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GIF_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_state_file() -> None:
    _ensure_dirs()
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps(DEFAULT_STATE), encoding="utf-8")


def _normalize_state(raw: dict) -> dict:
    state = DEFAULT_STATE.copy()
    state.update(raw or {})
    state["type"] = "gif" if state.get("type") == "gif" else "text"
    state["text"] = str(state.get("text", ""))[:200]
    state["gif_url"] = str(state.get("gif_url", ""))[:500]
    state["gif_id"] = str(state.get("gif_id", ""))
    state["frame_count"] = int(state.get("frame_count", 0) or 0)
    state["frame_delay_ms"] = int(state.get("frame_delay_ms", 120) or 120)
    state["frame_width"] = int(state.get("frame_width", FRAME_WIDTH) or FRAME_WIDTH)
    state["frame_height"] = int(state.get("frame_height", FRAME_HEIGHT) or FRAME_HEIGHT)
    state["frame_pipeline_version"] = str(state.get("frame_pipeline_version", FRAME_PIPELINE_VERSION))
    return state


def _read_state() -> dict:
    _ensure_state_file()
    with _state_lock:
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = DEFAULT_STATE.copy()
            STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
    return _normalize_state(raw)


def _write_state(state: dict) -> None:
    _ensure_state_file()
    with _state_lock:
        STATE_PATH.write_text(json.dumps(_normalize_state(state), ensure_ascii=True), encoding="utf-8")


def _is_valid_token(token: str | None) -> bool:
    return token == API_TOKEN


def _fetch_json(url: str) -> dict | None:
    req = URLRequest(url, headers={"User-Agent": "esp-messager/1.0"})
    try:
        with urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("JSON fetch failed for %s: %s", url, exc)
        return None


def _pick_tenor_gif(media_formats: dict) -> str:
    for key in ("tinygif", "nanogif", "mediumgif", "gif"):
        candidate = (media_formats.get(key) or {}).get("url", "").strip()
        if candidate:
            return candidate
    return ""


def _resolve_tenor_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if ".gif" in url.lower() and "tenor.com/view" not in url.lower():
        return url
    if "tenor.com" not in url.lower():
        return url

    match = re.search(r"-gif-(\d+)", url)
    if TENOR_API_KEY and match:
        post_id = match.group(1)
        api_url = (
            "https://tenor.googleapis.com/v2/posts?"
            + urlencode({"key": TENOR_API_KEY, "ids": post_id, "media_filter": "tinygif,nanogif,mediumgif,gif"})
        )
        payload = _fetch_json(api_url)
        if payload:
            results = payload.get("results", [])
            if results:
                media_url = _pick_tenor_gif(results[0].get("media_formats") or {})
                if media_url:
                    logger.info("Resolved Tenor via API: %s -> %s", url, media_url)
                    return media_url

    req = URLRequest(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Tenor page fetch failed: %s", exc)
        return url

    media_match = re.search(r'https://media\d*\.tenor\.com/[^"]+\.gif', html)
    if media_match:
        media_url = media_match.group(0)
        logger.info("Resolved Tenor via HTML: %s -> %s", url, media_url)
        return media_url

    escaped_match = re.search(r'https:\\/\\/media\d*\\.tenor\\.com\\/[^"]+\\.gif', html)
    if escaped_match:
        media_url = escaped_match.group(0).replace("\\/", "/")
        logger.info("Resolved Tenor via escaped HTML: %s -> %s", url, media_url)
        return media_url

    return url


def _download_gif_bytes(url: str) -> bytes:
    req = URLRequest(url, headers={"User-Agent": "esp-messager/1.0"})
    with urlopen(req, timeout=15) as response:
        data = response.read(MAX_SOURCE_GIF_BYTES + 1)
    if len(data) > MAX_SOURCE_GIF_BYTES:
        raise ValueError("source_gif_too_large")
    if len(data) < 32:
        raise ValueError("source_gif_too_small")
    return data


def _rgb565_bytes(img: Image.Image) -> bytes:
    rgb = img.convert("RGB")
    src = rgb.tobytes()
    out = bytearray(FRAME_BYTES)
    o = 0
    for i in range(0, len(src), 3):
        r = src[i]
        g = src[i + 1]
        b = src[i + 2]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[o] = v & 0xFF
        out[o + 1] = (v >> 8) & 0xFF
        o += 2
    return bytes(out)


def _build_gif_frames(gif_url: str) -> tuple[str, int, int]:
    raw = _download_gif_bytes(gif_url)
    try:
        image = Image.open(io.BytesIO(raw))
    except Exception as exc:
        logger.warning("Invalid GIF payload: %s", exc)
        raise ValueError("invalid_gif") from exc

    total = max(1, getattr(image, "n_frames", 1))
    step = max(1, math.ceil(total / max(1, MAX_FRAMES)))
    gif_id = uuid.uuid4().hex[:12]
    target = GIF_DIR / gif_id
    target.mkdir(parents=True, exist_ok=True)

    sampled_indices = [i for i in range(total) if i % step == 0] or [0]
    delays = []
    frame_count = 0

    for index in sampled_indices:
        image.seek(index)
        duration = int(image.info.get("duration", 120) or 120)
        delays.append(duration)
        frame_rgb = image.convert("RGB")

        # HA kell rotáció, itt legyen (de egyszer forgasd csak)
        if FRAME_ROTATE_DEG in (90, 180, 270):
            frame_rgb = frame_rgb.rotate(FRAME_ROTATE_DEG, expand=True)

        # Hard no-stretch mode: keep aspect ratio and center.
        fitted = ImageOps.contain(frame_rgb, (FRAME_WIDTH, FRAME_HEIGHT), Image.BILINEAR)
        canvas = ImageOps.fit(
        frame_rgb,
        (FRAME_WIDTH, FRAME_HEIGHT),
        Image.BILINEAR,
        centering=(0.5, 0.5),
        )

        px = (FRAME_WIDTH - fitted.width) // 2
        py = (FRAME_HEIGHT - fitted.height) // 2
        canvas.paste(fitted, (px, py))


        rgb565 = _rgb565_bytes(canvas)
        frame_path = target / f"frame_{frame_count:03d}.rgb565"
        frame_path.write_bytes(rgb565)

        frame_count += 1


    if frame_count == 0:
        raise ValueError("no_frames")

    avg_delay = int(sum(delays) / len(delays)) if delays else 120
    delay_ms = max(60, min(300, avg_delay * step))

    meta = {
        "gif_id": gif_id,
        "frame_count": frame_count,
        "frame_delay_ms": delay_ms,
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
        "frame_pipeline_version": FRAME_PIPELINE_VERSION,
        "source_url": gif_url,
    }
    (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=True), encoding="utf-8")
    logger.info("GIF built gif_id=%s frames=%s delay=%sms", gif_id, frame_count, delay_ms)
    return gif_id, frame_count, delay_ms


def _refresh_gif_if_stale(state: dict) -> dict:
    if state.get("type") != "gif":
        return state
    if (
        int(state.get("frame_width", 0)) == FRAME_WIDTH
        and int(state.get("frame_height", 0)) == FRAME_HEIGHT
        and str(state.get("frame_pipeline_version", "")) == FRAME_PIPELINE_VERSION
    ):
        return state

    source_url = str(state.get("gif_url", "")).strip()
    if not source_url:
        state["gif_id"] = ""
        state["frame_count"] = 0
        state["frame_delay_ms"] = 120
        state["frame_width"] = FRAME_WIDTH
        state["frame_height"] = FRAME_HEIGHT
        state["frame_pipeline_version"] = FRAME_PIPELINE_VERSION
        _write_state(state)
        return state

    logger.info(
        "Refreshing stale GIF cache for new frame size (%sx%s -> %sx%s)",
        state.get("frame_width"),
        state.get("frame_height"),
        FRAME_WIDTH,
        FRAME_HEIGHT,
    )
    try:
        gif_id, frame_count, frame_delay_ms = _build_gif_frames(source_url)
    except ValueError as exc:
        logger.warning("Failed to refresh stale GIF cache: %s", exc)
        state["gif_id"] = ""
        state["frame_count"] = 0
        state["frame_delay_ms"] = 120
        state["frame_width"] = FRAME_WIDTH
        state["frame_height"] = FRAME_HEIGHT
        state["frame_pipeline_version"] = FRAME_PIPELINE_VERSION
        _write_state(state)
        return state

    state["gif_id"] = gif_id
    state["frame_count"] = frame_count
    state["frame_delay_ms"] = frame_delay_ms
    state["frame_width"] = FRAME_WIDTH
    state["frame_height"] = FRAME_HEIGHT
    state["frame_pipeline_version"] = FRAME_PIPELINE_VERSION
    _write_state(state)
    return state


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, token: str | None = None):
    authed = _is_valid_token(token)
    state = _read_state() if authed else DEFAULT_STATE
    if authed:
        state = _refresh_gif_if_stale(state)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "authed": authed,
            "token": token or "",
            "state": state,
        },
    )


@app.post("/ui/set")
def ui_set(token: str = Form(...), text: str = Form("")):
    if not _is_valid_token(token):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    value = (text or "").strip()[:200]
    _write_state(
        {
            "type": "text",
            "text": value,
            "gif_url": "",
            "gif_id": "",
            "frame_count": 0,
            "frame_delay_ms": 120,
            "frame_width": FRAME_WIDTH,
            "frame_height": FRAME_HEIGHT,
            "frame_pipeline_version": FRAME_PIPELINE_VERSION,
        }
    )
    logger.info("Text message updated (%s chars)", len(value))
    return {"ok": True}


@app.post("/ui/gif")
def ui_gif(token: str = Form(...), gif_url: str = Form("")):
    if not _is_valid_token(token):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})

    resolved = _resolve_tenor_url((gif_url or "").strip()[:500])
    if "tenor.com/view/" in resolved.lower() or ".gif" not in resolved.lower():
        logger.warning("GIF URL unresolved or non-gif: %s", resolved)
        return JSONResponse(
            status_code=400,
            content={
                "error": "gif_unresolved",
                "message": "Could not resolve Tenor URL to a direct .gif URL.",
            },
        )

    try:
        gif_id, frame_count, frame_delay_ms = _build_gif_frames(resolved)
    except ValueError as exc:
        logger.warning("GIF processing failed: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "error": "gif_processing_failed",
                "message": "Could not process GIF. Try a different GIF URL.",
            },
        )

    _write_state(
        {
            "type": "gif",
            "text": "",
            "gif_url": resolved,
            "gif_id": gif_id,
            "frame_count": frame_count,
            "frame_delay_ms": frame_delay_ms,
            "frame_width": FRAME_WIDTH,
            "frame_height": FRAME_HEIGHT,
            "frame_pipeline_version": FRAME_PIPELINE_VERSION,
        }
    )
    logger.info("GIF updated: %s -> id=%s", resolved, gif_id)
    return {"ok": True, "gif_id": gif_id, "frame_count": frame_count, "frame_delay_ms": frame_delay_ms}


@app.get("/msg")
def msg(token: str | None = None):
    if not _is_valid_token(token):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    state = _read_state()
    state = _refresh_gif_if_stale(state)
    if state.get("type") == "gif":
        return {
            "type": "gif",
            "gif_url": state.get("gif_url", ""),
            "gif_id": state.get("gif_id", ""),
            "frame_count": int(state.get("frame_count", 0)),
            "frame_delay_ms": int(state.get("frame_delay_ms", 120)),
            "frame_width": int(state.get("frame_width", FRAME_WIDTH)),
            "frame_height": int(state.get("frame_height", FRAME_HEIGHT)),
            "frame_pipeline_version": str(state.get("frame_pipeline_version", FRAME_PIPELINE_VERSION)),
        }
    return {"type": "text", "text": state.get("text", "")}


@app.get("/gif/frame")
def gif_frame(token: str | None = None, gif_id: str = "", i: int = 0):
    if not _is_valid_token(token):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    if not gif_id or i < 0 or i > 999:
        return JSONResponse(status_code=400, content={"error": "bad_request"})

    path = GIF_DIR / gif_id / f"frame_{i:03d}.rgb565"
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "not_found"})

    return FileResponse(path, media_type="application/octet-stream")


@app.get("/tenor/search")
def tenor_search(q: str = "", token: str | None = None):
    if not TENOR_FEATURE_ENABLED:
        return JSONResponse(status_code=404, content={"error": "feature_disabled"})
    if not _is_valid_token(token):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    if not TENOR_API_KEY:
        return JSONResponse(status_code=503, content={"error": "missing_tenor_api_key"})

    query = (q or "").strip()
    if not query:
        return {"results": []}

    params = urlencode(
        {
            "key": TENOR_API_KEY,
            "q": query,
            "limit": 8,
            "media_filter": "tinygif,nanogif,mediumgif,gif",
        }
    )
    url = f"https://tenor.googleapis.com/v2/search?{params}"

    payload = _fetch_json(url)
    if payload is None:
        logger.warning("Tenor lookup failed for query: %s", query)
        return JSONResponse(status_code=502, content={"error": "tenor_lookup_failed"})

    results = []
    for item in payload.get("results", []):
        media_url = _pick_tenor_gif(item.get("media_formats") or {})
        if media_url:
            results.append({"title": item.get("content_description", ""), "url": media_url})
        if len(results) >= 8:
            break

    return {"results": results}
