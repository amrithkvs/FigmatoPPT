"""Figma REST API helpers: parse a file URL, list its top-level frames,
and render those frames to PNG images."""

import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote

import requests

API = "https://api.figma.com/v1"
DEFAULT_RENDER_SCALE = max(1, int(os.environ.get("FIGMA_RENDER_SCALE", "1")))
IMAGE_BATCH_SIZE = max(1, int(os.environ.get("FIGMA_IMAGE_BATCH_SIZE", "20")))

# https://www.figma.com/design/<KEY>/Title   (also /file/, /board/, /proto/)
_KEY_RE = re.compile(r"figma\.com/(?:file|design|board|proto)/([A-Za-z0-9]+)")
_NODE_RE = re.compile(r"node-id=([^&]+)")


def parse_file_key(url: str) -> str:
    """Pull the file key out of any Figma file/design URL."""
    m = _KEY_RE.search(url or "")
    if not m:
        raise ValueError("That doesn't look like a Figma file URL.")
    return m.group(1)


def parse_node_id(url: str):
    """Pull the selected node id out of a Figma URL (?node-id=65-9358 -> '65:9358').
    Returns None if absent. Used to scope a deck to just the page in the URL."""
    m = _NODE_RE.search(url or "")
    if not m:
        return None
    return unquote(m.group(1)).replace("-", ":")


def _headers(token: str) -> dict:
    return {"X-Figma-Token": token}


def _get_with_retries(url: str, *, headers: dict, params: Optional[dict] = None, timeout: int = 60, retries: int = 3):
    """GET with small retry/backoff for transient failures and rate limits."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            if 500 <= r.status_code < 600:
                time.sleep(1 + attempt)
                continue
            return r
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(1 + attempt)
    if last_err:
        raise last_err
    raise RuntimeError("Request failed unexpectedly without a response.")


def validate_token(token: str) -> None:
    """Quick check that a personal access token works, for save-time feedback."""
    r = requests.get(f"{API}/me", headers=_headers(token), timeout=30)
    if r.status_code in (401, 403):
        raise PermissionError("Figma rejected that token. Double-check it and try again.")
    r.raise_for_status()


def list_frames(file_key: str, token: str, page_node_id: str = None, include_version: bool = False):
    """Return top-level frames as {id, name, page}. If page_node_id is given (the
    node from the file URL), return only frames from the page that contains it.
    When include_version=True, returns (frames, file_version)."""
    try:
        r = _get_with_retries(
            f"{API}/files/{file_key}",
            headers=_headers(token),
            params={"depth": 2},
            timeout=60,
            retries=3,
        )
    except requests.exceptions.ReadTimeout as e:
        raise TimeoutError("Figma API timed out while reading the file. Please retry in a moment.") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Could not reach Figma API: {e}") from e
    if r.status_code == 403:
        raise PermissionError("Figma rejected the token (403). Check the access token.")
    if r.status_code == 404:
        raise FileNotFoundError("Figma file not found (404). Check the URL and token access.")
    r.raise_for_status()

    payload = r.json()
    doc = payload["document"]
    pages = [p for p in doc.get("children", []) if p.get("type") == "CANVAS"]

    # Scope to just the selected page when the URL points at one.
    if page_node_id:
        match = next((p for p in pages if p.get("id") == page_node_id or _contains(p, page_node_id)), None)
        if match:
            pages = [match]

    frames = []
    for page in pages:
        page_frames = [n for n in page.get("children", []) if n.get("type") == "FRAME"]

        # Order slides the way a human reads the canvas: top-to-bottom, then left-to-right.
        def pos(n):
            b = n.get("absoluteBoundingBox") or {}
            return (round(b.get("y", 0) / 50), b.get("x", 0))  # row-banded, then column

        page_frames.sort(key=pos)
        for node in page_frames:
            frames.append({"id": node["id"], "name": node.get("name", "Frame"), "page": page.get("name", "")})
    if include_version:
        return frames, payload.get("version")
    return frames


def _contains(node: dict, target_id: str) -> bool:
    """True if target_id is this node or any descendant."""
    if node.get("id") == target_id:
        return True
    return any(_contains(c, target_id) for c in node.get("children", []))


def fetch_nodes(file_key: str, token: str, node_ids: list[str]) -> dict:
    """Fetch the full node subtree (geometry, fills, text, fonts) for each id.
    Returns {node_id: <document subtree>}."""
    out: dict[str, dict] = {}
    for i in range(0, len(node_ids), 50):
        batch = node_ids[i : i + 50]
        r = _get_with_retries(
            f"{API}/files/{file_key}/nodes",
            headers=_headers(token),
            params={"ids": ",".join(batch)},
            timeout=120,
            retries=3,
        )
        r.raise_for_status()
        nodes = r.json().get("nodes", {})
        for nid in batch:
            entry = nodes.get(nid)
            if entry and entry.get("document"):
                out[nid] = entry["document"]
    return out


def render_frames(file_key: str, token: str, node_ids: list[str], scale: int = DEFAULT_RENDER_SCALE) -> dict[str, bytes]:
    """Render the given node ids to PNG. Returns {node_id: png_bytes}, preserving order."""
    if not node_ids:
        return {}

    # Large raster groups are typically the slowest part. Use a lower scale for
    # big batches to keep generation snappy and reduce render timeouts.
    effective_scale = 1 if len(node_ids) >= 8 else scale

    # Ask Figma to render the nodes. The render step (server-side) is the slow
    # part, so run the batches concurrently — with 429-aware backoff so we don't
    # fail the whole build if Figma rate-limits us.
    batches = [node_ids[i : i + IMAGE_BATCH_SIZE] for i in range(0, len(node_ids), IMAGE_BATCH_SIZE)]
    url_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for part in ex.map(lambda b: _render_batch(file_key, token, b, effective_scale), batches):
            url_map.update(part)

    # Then download the rendered PNGs in parallel.
    out: dict[str, bytes] = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for nid, data in ex.map(lambda it: (it[0], _download(it[1])), url_map.items()):
            out[nid] = data

    # Preserve the caller's order.
    return {nid: out[nid] for nid in node_ids if nid in out}


def _render_batch(file_key: str, token: str, batch: list[str], scale: int, retries: int = 4) -> dict[str, str]:
    """Render one batch of node ids to image URLs, retrying on 429 rate limits."""
    for attempt in range(retries):
        r = requests.get(
            f"{API}/images/{file_key}",
            headers=_headers(token),
            params={"ids": ",".join(batch), "format": "png", "scale": scale},
            timeout=120,
        )
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 400:
            payload = {}
            try:
                payload = r.json() or {}
            except Exception:  # noqa: BLE001
                payload = {}
            err = str(payload.get("err") or payload.get("message") or "")
            # For heavy decks, 400s are often render-time or bad-node issues.
            # Split first to isolate offenders, then lower scale for single nodes.
            if len(batch) > 1:
                mid = len(batch) // 2
                left = _render_batch(file_key, token, batch[:mid], scale, retries)
                right = _render_batch(file_key, token, batch[mid:], scale, retries)
                merged = {}
                merged.update(left)
                merged.update(right)
                return merged
            if scale > 1:
                return _render_batch(file_key, token, batch, scale=1, retries=retries)
            # If one node still returns 400 even at scale 1, skip it so the deck
            # can still finish.
            return {}
        r.raise_for_status()
        payload = r.json()
        if payload.get("err"):
            raise RuntimeError(f"Figma render error: {payload['err']}")
        return {nid: u for nid, u in payload.get("images", {}).items() if u}
    raise RuntimeError("Figma rate-limited image rendering repeatedly (429). Try again shortly.")


def create_webhook(token: str, file_key: str, endpoint: str, passcode: str) -> str:
    """Register a FILE_UPDATE webhook scoped to this file. Returns the webhook id.
    Raises with a readable message if Figma rejects it (plan/permissions/context)."""
    r = requests.post(
        f"{API.replace('/v1', '/v2')}/webhooks",
        headers=_headers(token),
        json={
            "event_type": "FILE_UPDATE",
            "context": "file",
            "context_id": file_key,
            "endpoint": endpoint,
            "passcode": passcode,
            "description": "figpoint live sync",
        },
        timeout=60,
    )
    if not r.ok:
        # Surface Figma's own reason (e.g. unsupported plan / bad endpoint).
        try:
            reason = r.json().get("message") or r.text
        except Exception:  # noqa: BLE001
            reason = r.text
        raise RuntimeError(f"Figma webhook setup failed ({r.status_code}): {reason}")
    return str(r.json().get("id"))


def delete_webhook(token: str, webhook_id: str) -> None:
    try:
        requests.delete(
            f"{API.replace('/v1', '/v2')}/webhooks/{webhook_id}",
            headers=_headers(token),
            timeout=30,
        )
    except Exception:  # noqa: BLE001 — best effort cleanup
        pass


def _download(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to download rendered image: {last}")
