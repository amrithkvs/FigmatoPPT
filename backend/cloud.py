"""Microsoft Graph (OneDrive / SharePoint) sync.

Connect a Microsoft account via OAuth, then upload each rebuilt deck so the cloud
copy refreshes in PowerPoint for the web. Everything here is a safe no-op unless
the app is configured (MS_CLIENT_ID set) AND an account is connected — so the
rest of the app works fine without it.

Setup (one-time, in Azure): register an app, set the redirect URI to
<base>/auth/ms/callback, grant delegated Graph scopes (User.Read,
Files.ReadWrite, offline_access), then set MS_CLIENT_ID / MS_CLIENT_SECRET.
"""

import base64
import hashlib
import os
import secrets
import time
from urllib.parse import urlencode

import requests

import store

# Pending PKCE verifiers, keyed by OAuth state (login -> callback handoff).
_pkce: dict[str, str] = {}

CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
TENANT = os.environ.get("MS_TENANT", "common")  # "common" = work or personal accounts
REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8000/auth/ms/callback")
LOGIN_HINT = os.environ.get("MS_LOGIN_HINT", "").strip()
SCOPES = "offline_access User.Read Files.ReadWrite"

AUTH_BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
FOLDER = "FigPoint"  # OneDrive folder that holds the synced decks


def is_configured() -> bool:
    return bool(CLIENT_ID)


def status() -> dict:
    acct = store.get_ms_account()
    return {
        "configured": is_configured(),
        "connected": bool(acct),
        "account": acct["name"] if acct else None,
    }


# ---------- OAuth ----------

def auth_url(state: str) -> str:
    # PKCE: corp tenants block client secrets, so we authenticate as a public
    # client with a one-time code_verifier/challenge instead of a secret.
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _pkce[state] = verifier
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "prompt": "select_account",
        "domain_hint": "organizations",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if LOGIN_HINT:
        params["login_hint"] = LOGIN_HINT
    q = urlencode(params)
    return f"{AUTH_BASE}/authorize?{q}"


def handle_callback(code: str, state: str = "") -> str:
    data = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    verifier = _pkce.pop(state, None)
    if verifier:
        data["code_verifier"] = verifier
    if CLIENT_SECRET:  # supported but not used on secret-blocked tenants
        data["client_secret"] = CLIENT_SECRET
    r = requests.post(f"{AUTH_BASE}/token", data=data, timeout=30)
    r.raise_for_status()
    tok = r.json()
    name = _whoami(tok["access_token"])
    store.set_ms_account(
        tok["access_token"], tok.get("refresh_token"),
        time.time() + tok.get("expires_in", 3600), name,
    )
    return name


def disconnect() -> None:
    store.clear_ms_account()


def _whoami(access_token: str) -> str:
    try:
        r = requests.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        if r.ok:
            j = r.json()
            return j.get("displayName") or j.get("userPrincipalName") or "Microsoft account"
    except Exception:  # noqa: BLE001
        pass
    return "Microsoft account"


def _valid_token():
    """Return a non-expired access token, refreshing if needed. None if not connected."""
    acct = store.get_ms_account()
    if not acct:
        return None
    if acct["expires_at"] and acct["expires_at"] - 60 > time.time():
        return acct["access_token"]
    if not acct.get("refresh_token"):
        return acct["access_token"]
    data = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "grant_type": "refresh_token",
        "refresh_token": acct["refresh_token"],
        "redirect_uri": REDIRECT_URI,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    r = requests.post(f"{AUTH_BASE}/token", data=data, timeout=30)
    if not r.ok:
        return None
    tok = r.json()
    store.set_ms_account(
        tok["access_token"], tok.get("refresh_token", acct["refresh_token"]),
        time.time() + tok.get("expires_in", 3600), acct["name"],
    )
    return tok["access_token"]


# ---------- upload ----------

def sync_deck(deck_id: int, local_path: str) -> dict:
    """Upload the deck to OneDrive (replacing the same item) and store its web edit
    link. No-op if not connected; never raises into the build."""
    try:
        if not store.get_ms_account():
            return {"synced": False}
        token = _valid_token()
        if not token:
            return {"synced": False, "error": "Microsoft sign-in expired — reconnect."}
        remote = f"{FOLDER}/{os.path.basename(local_path)}"
        item = _upload(token, local_path, remote)
        web_url = _edit_link(token, item["id"]) or item.get("webUrl")
        store.set_deck_cloud(deck_id, web_url, item["id"])
        print(f"[cloud] synced deck {deck_id} -> {web_url}", flush=True)
        return {"synced": True, "web_url": web_url}
    except Exception as e:  # noqa: BLE001
        print(f"[cloud] sync failed for deck {deck_id}: {e}", flush=True)
        return {"synced": False, "error": str(e)}


def _upload(token: str, local_path: str, remote_path: str) -> dict:
    """Upload via a resumable session (handles large files), replacing any existing."""
    headers = {"Authorization": f"Bearer {token}"}
    size = os.path.getsize(local_path)
    sess = requests.post(
        f"{GRAPH}/me/drive/root:/{remote_path}:/createUploadSession",
        headers=headers,
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=30,
    )
    sess.raise_for_status()
    upload_url = sess.json()["uploadUrl"]

    chunk = 5 * 320 * 1024  # 1.6 MiB — Graph requires a multiple of 320 KiB
    item = None
    with open(local_path, "rb") as f:
        start = 0
        while start < size:
            data = f.read(chunk)
            end = start + len(data) - 1
            r = requests.put(
                upload_url,
                headers={"Content-Length": str(len(data)),
                         "Content-Range": f"bytes {start}-{end}/{size}"},
                data=data, timeout=120,
            )
            r.raise_for_status()
            if r.status_code in (200, 201):
                item = r.json()
            start = end + 1

    if not item:  # session completed without returning the item; fetch it
        meta = requests.get(f"{GRAPH}/me/drive/root:/{remote_path}", headers=headers, timeout=30)
        meta.raise_for_status()
        item = meta.json()
    return item


def _edit_link(token: str, item_id: str):
    """An org-shareable edit link that opens in PowerPoint for the web."""
    try:
        r = requests.post(
            f"{GRAPH}/me/drive/items/{item_id}/createLink",
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "edit", "scope": "organization"},
            timeout=30,
        )
        if r.ok:
            return r.json().get("link", {}).get("webUrl")
    except Exception:  # noqa: BLE001
        pass
    return None
