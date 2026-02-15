import os
import sqlite3
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

APP_TITLE = "ESP-MESSAGER"
DB_PATH = os.getenv("DB_PATH", "/data/messages.db")
API_TOKEN = os.getenv("API_TOKEN", "changeme")  # állítsd át compose-ban

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory="app/templates")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv (
          k TEXT PRIMARY KEY,
          v TEXT NOT NULL
        )
        """
    )
    return conn


def get_value(key: str, default: str = "") -> str:
    conn = db()
    try:
        cur = conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_value(key: str, value: str) -> None:
    conn = db()
    try:
        conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def require_token(token: str | None):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/health")
def health():
    return {"ok": True}


# ESP32 ezt hívja
@app.get("/msg")
def get_msg(token: str | None = None):
    require_token(token)
    text = get_value("text", "Szia :)")
    return {"text": text}


# Telefon/web UI ezt használja (vagy curl)
@app.post("/set")
def set_msg(text: str, token: str | None = None):
    require_token(token)
    text = (text or "").strip()[:200]
    set_value("text", text)
    return {"ok": True, "text": text}


# Mini web UI (jelszó: token)
@app.get("/", response_class=HTMLResponse)
def home(request: Request, token: str | None = None):
    # ha nem adod meg a tokent, csak egy "token kell" oldalt adunk
    if token != API_TOKEN:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "token": "", "text": "", "authed": False},
        )

    text = get_value("text", "Szia :)")
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "token": token, "text": text, "authed": True},
    )


@app.post("/ui/set")
def ui_set(token: str = Form(...), text: str = Form(...)):
    # form submit
    require_token(token)
    text = (text or "").strip()[:200]
    set_value("text", text)
    return RedirectResponse(url=f"/?token={token}", status_code=303)
