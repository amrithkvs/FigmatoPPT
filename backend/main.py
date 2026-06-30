"""FastAPI app: create decks from Figma, generate the .pptx now, and
regenerate on a daily schedule (default midnight + 5pm)."""

import os
import re
import secrets
import subprocess
import sys
import time
import traceback
from typing import List, Optional, Set

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

import figma
import pptx_native
import store
import cloud

def _resource(*parts):
    # Resolve bundled read-only files (the frontend) in both dev and packaged modes.
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.join(os.path.dirname(__file__), "..")
    return os.path.join(base, *parts)


FRONTEND = _resource("frontend", "index.html")
POWERPOINT_SVG = _resource("PowerPoint.svg")
FIGMA_LOGO = _resource("Figma-logo.svg.webp")
MICROSOFT_LOGO = _resource("Microsoft-logo.svg")

# The live, auto-updating deck files live in a clean, discoverable folder that the
# user opens directly — NOT inside the project, and NOT a frozen browser-download
# copy. Override with DECKS_OUTPUT_DIR if you want them somewhere else.
OUTPUT_DIR = os.environ.get("DECKS_OUTPUT_DIR") or os.path.expanduser("~/Documents/Figma Decks")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Public HTTPS base URL Figma can reach (e.g. a cloudflared tunnel or the
# deployed host). Without it, live-sync can't be registered and decks fall
# back to the daily safety refresh.
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")

app = FastAPI(title="Figma → Deck")
scheduler = BackgroundScheduler()
_ms_oauth_states: Set[str] = set()


# ---------- file helpers ----------

def _safe_name(name: str) -> str:
    """A filesystem-safe deck name, so the .pptx has a clean, stable filename."""
    return re.sub(r"[^\w\- ]", "", name or "").strip() or "deck"


def deck_path(deck: dict) -> str:
    """The single live file for a deck, in ~/Documents/Figma Decks. Overwritten on
    every rebuild, so the user opens ONE stable file that always reflects Figma —
    not a frozen download copy."""
    return os.path.join(OUTPUT_DIR, f"{_safe_name(deck['name'])}.pptx")


# ---------- core ----------

def generate(deck_id: int) -> dict:
    """Render the deck's Figma frames and (re)build its .pptx. Records status."""
    deck = store.get_deck(deck_id, with_secrets=True)
    if not deck:
        raise ValueError("deck not found")
    try:
        store.update_progress(deck_id, 0)
        page = figma.parse_node_id(deck["figma_url"])  # scope to the page in the URL
        frames = figma.list_frames(deck["file_key"], deck["token"], page)
        if not frames:
            store.record_run(deck_id, "no frames found")
            return {"ok": False, "error": "No top-level frames found in that file."}

        # Pull each frame's full node tree, then rebuild it as editable shapes.
        t0 = time.time()
        print(f"[deck {deck_id}] building {len(frames)} frames…", flush=True)
        store.update_progress(deck_id, 15)
        detail = figma.fetch_nodes(deck["file_key"], deck["token"], [f["id"] for f in frames])
        store.update_progress(deck_id, 30)
        ordered = [detail[f["id"]] for f in frames if f["id"] in detail]

        out_path = deck_path(deck)
        def _progress(done, all_frames):
            capped_total = max(1, all_frames)
            pct = 30 + round((done / capped_total) * 65)
            store.update_progress(deck_id, min(95, pct))

        count = pptx_native.build_deck(deck["file_key"], deck["token"], ordered, out_path, progress_cb=_progress)
        store.update_progress(deck_id, 99)
        store.record_run(deck_id, "ok", slide_count=count, pptx_path=out_path)
        # Best effort cloud sync for PowerPoint web open; never fails the build.
        cloud.sync_deck(deck_id, out_path)
        print(f"[deck {deck_id}] done: {count} slides in {time.time() - t0:.1f}s", flush=True)
        return {"ok": True, "slides": count}
    except Exception as e:  # noqa: BLE001
        store.record_run(deck_id, f"error: {e}")
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def _run_at(hhmm: str):
    """Build a cron trigger from 'HH:MM'."""
    hour, minute = hhmm.split(":")
    return CronTrigger(hour=int(hour), minute=int(minute))


def schedule_deck(deck: dict):
    """Register one APScheduler job per scheduled time for this deck (safety net)."""
    for t in deck["times"]:
        scheduler.add_job(
            generate,
            trigger=_run_at(t),
            args=[deck["id"]],
            id=f"deck-{deck['id']}-{t}",
            replace_existing=True,
        )


def register_live_sync(deck_id: int) -> dict:
    """Best-effort: register a Figma webhook so the deck rebuilds on file change."""
    if not WEBHOOK_BASE_URL:
        return {"active": False, "reason": "No public URL set (WEBHOOK_BASE_URL) — using daily refresh."}
    deck = store.get_deck(deck_id, with_secrets=True)
    passcode = secrets.token_hex(16)
    try:
        webhook_id = figma.create_webhook(
            deck["token"], deck["file_key"], f"{WEBHOOK_BASE_URL}/api/webhook/figma", passcode
        )
        store.set_webhook(deck_id, webhook_id, passcode)
        return {"active": True}
    except Exception as e:  # noqa: BLE001
        return {"active": False, "reason": str(e)}


# ---------- api ----------

class DeckIn(BaseModel):
    figma_url: str
    token: Optional[str] = None  # optional — falls back to the saved token
    name: Optional[str] = None
    times: Optional[List[str]] = None  # ["HH:MM", ...]; defaults to midnight + 5pm


class TokenIn(BaseModel):
    token: str


@app.on_event("startup")
def _startup():
    store.init()
    scheduler.start()
    for deck in store.all_decks_with_secrets():
        import json
        deck["times"] = json.loads(deck["times"])
        schedule_deck(deck)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(FRONTEND) as f:
        return f.read()


@app.get("/assets/powerpoint.svg")
def powerpoint_svg():
    return FileResponse(POWERPOINT_SVG, media_type="image/svg+xml")


@app.get("/assets/figma-logo.webp")
def figma_logo():
    return FileResponse(FIGMA_LOGO, media_type="image/webp")


@app.get("/assets/microsoft-logo.svg")
def microsoft_logo():
    return FileResponse(MICROSOFT_LOGO, media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(MICROSOFT_LOGO, media_type="image/svg+xml")


@app.get("/api/config")
def get_config():
    return {"output_dir": OUTPUT_DIR}


def _mask_token(t: str) -> str:
    return f"{t[:6]}…{t[-4:]}" if t and len(t) > 12 else "saved"


@app.get("/api/figma-token")
def figma_token_status():
    t = store.get_figma_token()
    return {"saved": bool(t), "hint": _mask_token(t) if t else None}


@app.post("/api/figma-token")
def save_figma_token(body: TokenIn):
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is empty.")
    # Don't validate against /v1/me here — that needs a user-profile scope we don't
    # ask for. The token only needs File-content access, which is verified for real
    # when a deck is created (list_frames). Just save it.
    store.set_figma_token(token)
    return {"saved": True, "hint": _mask_token(token)}


@app.get("/api/decks")
def get_decks():
    return store.list_decks()


@app.get("/api/ms/status")
def ms_status():
    return cloud.status()


@app.get("/api/ms/connect")
def ms_connect():
    if not cloud.is_configured():
        raise HTTPException(status_code=400, detail="Microsoft cloud sync is not configured on this server.")
    state = secrets.token_urlsafe(24)
    _ms_oauth_states.add(state)
    return RedirectResponse(cloud.auth_url(state), status_code=302)


@app.get("/auth/ms/callback", response_class=HTMLResponse)
def ms_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return f"<html><body><script>window.location='/?ms_error={error}';</script></body></html>"

    if not code or not state or state not in _ms_oauth_states:
        raise HTTPException(status_code=400, detail="Invalid Microsoft OAuth callback state.")

    _ms_oauth_states.discard(state)
    try:
        cloud.handle_callback(code, state)
        return "<html><body><script>window.location='/?ms_connected=1';</script></body></html>"
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Microsoft connect failed: {e}")


@app.post("/api/ms/disconnect")
def ms_disconnect():
    cloud.disconnect()
    return {"ok": True}


@app.post("/api/decks")
def create_deck(body: DeckIn, bg: BackgroundTasks):
    token = body.token or store.get_figma_token()
    if not token:
        raise HTTPException(status_code=400, detail="Save your Figma token first.")
    try:
        file_key = figma.parse_file_key(body.figma_url)
        page = figma.parse_node_id(body.figma_url)  # scope to the page in the URL
        frames = figma.list_frames(file_key, token, page)  # validates token + access up front
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (ValueError, PermissionError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    if body.token:
        store.set_figma_token(body.token)  # remember a freshly-entered token

    times = body.times or ["03:00"]  # daily safety net; live-sync is primary
    name = body.name or (frames[0]["page"] if frames else "Untitled deck")
    deck_id = store.add_deck(name, body.figma_url, file_key, token, times)
    store.mark_building(deck_id)

    deck = store.get_deck(deck_id)
    schedule_deck(deck)
    webhook = register_live_sync(deck_id)  # rebuild on every Figma change
    bg.add_task(generate, deck_id)  # build in the background; the UI polls for it
    return {"deck": store.get_deck(deck_id), "live_sync": webhook}


@app.post("/api/webhook/figma")
async def figma_webhook(req: Request, bg: BackgroundTasks):
    """Figma calls this on file changes (and once with PING at registration)."""
    body = await req.json()
    event = body.get("event_type")
    if event == "PING":
        return {"ok": True}
    if event == "FILE_UPDATE":
        decks = store.find_decks_for_webhook(body.get("file_key"), body.get("passcode"))
        for d in decks:
            bg.add_task(generate, d["id"])  # rebuild without blocking Figma's request
        return {"ok": True, "rebuilding": len(decks)}
    return {"ok": True}


@app.post("/api/decks/{deck_id}/generate")
def regenerate(deck_id: int, bg: BackgroundTasks):
    deck = store.get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="deck not found")
    if deck.get("last_status") == "building":
        return {"started": False, "reason": "already building"}
    store.mark_building(deck_id)  # show "Building…" while it rebuilds
    bg.add_task(generate, deck_id)
    return {"started": True}


@app.get("/api/decks/{deck_id}/download")
def download(deck_id: int):
    deck = store.get_deck(deck_id)
    if not deck or not deck.get("pptx_path") or not os.path.exists(deck["pptx_path"]):
        raise HTTPException(status_code=404, detail="No deck generated yet.")
    fname = f"{deck['name'].replace('/', '-')}.pptx"
    return FileResponse(deck["pptx_path"], filename=fname)


def _existing_path(deck_id: int) -> str:
    deck = store.get_deck(deck_id)
    if not deck or not deck.get("pptx_path") or not os.path.exists(deck["pptx_path"]):
        raise HTTPException(status_code=404, detail="No deck generated yet.")
    return deck["pptx_path"]


@app.post("/api/decks/{deck_id}/open")
def open_deck(deck_id: int):
    """Open the deck's single canonical file in PowerPoint on this machine. Every
    rebuild overwrites that same file — no pile-up of downloaded copies."""
    path = _existing_path(deck_id)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])
    return {"ok": True, "path": path}


@app.post("/api/decks/{deck_id}/reveal")
def reveal_deck(deck_id: int):
    """Reveal the deck file in Finder/Explorer."""
    path = _existing_path(deck_id)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
    return {"ok": True, "path": path}


@app.delete("/api/decks/{deck_id}")
def remove(deck_id: int):
    deck = store.get_deck(deck_id, with_secrets=True)
    if deck and deck.get("webhook_id"):
        figma.delete_webhook(deck["token"], deck["webhook_id"])  # best effort
    if deck and deck.get("pptx_path") and os.path.exists(deck["pptx_path"]):
        try:
            os.remove(deck["pptx_path"])  # don't leave orphaned files behind
        except OSError:
            pass
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(f"deck-{deck_id}-"):
            job.remove()
    store.delete_deck(deck_id)
    return {"ok": True}
