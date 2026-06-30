# Figma → Deck

Point it at a Figma file once. It builds an **editable** PowerPoint — real text
and shapes, not flat images — and **rebuilds automatically whenever the Figma
file changes** (plus a daily safety refresh).

## Run

```bash
./run.sh
```

Then open **http://localhost:8000**, paste a Figma file URL and a personal
access token (Figma → Settings → Security → Personal access tokens), and click
**Create PPT deck**. The deck is built immediately.

## Share with colleagues (hosted URL)

If you want teammates to use this without downloading a zip or running locally,
deploy once and share the app URL.

### Fastest path: Render (included config)

This repo now includes:

- `Dockerfile`
- `.dockerignore`
- `render.yaml`

Steps:

1. Push this repo to GitHub.
2. In Render, create a new Blueprint deployment from that repo.
3. Set required environment variables in Render:
   - `WEBHOOK_BASE_URL=https://<your-render-app-domain>`
  - `FIGMA_DECK_DB_PATH=/data/decks/data.db`
   - `MS_CLIENT_ID` / `MS_TENANT` / `MS_REDIRECT_URI` (optional, for web PowerPoint sync)
4. Deploy and share the Render URL with colleagues.

Notes:

- Persistent disk is configured in `render.yaml` (`/data`) for deck files and DB.
- Everyone uses the same hosted app in browser; no local setup needed.
- If you need org SSO and restricted access, place the app behind your corporate
  access control (for example, Cloudflare Access, Entra Application Proxy, or
  your internal ingress).

## Live sync (rebuild on every Figma change)

Live sync uses a Figma **webhook**, so Figma must be able to reach this server
over **public HTTPS**. Set `WEBHOOK_BASE_URL` to that public URL before starting.

Local testing with a tunnel:

```bash
# terminal 1 — expose the local server publicly
cloudflared tunnel --url http://localhost:8000     # prints https://<random>.trycloudflare.com

# terminal 2 — start the app pointed at that URL
WEBHOOK_BASE_URL=https://<random>.trycloudflare.com ./run.sh
```

When hosted, set `WEBHOOK_BASE_URL` to the deployed origin. If it's unset, decks
still work but fall back to the daily refresh (the UI shows "Daily refresh"
instead of "Live sync").

**Honest caveats:**
- Figma throttles `FILE_UPDATE` — roughly **once per 30 min while editing**, plus
  once shortly after editing stops. So updates are near-instant *after a pause*,
  not per keystroke. This is a Figma platform limit.
- Webhooks may require team-admin rights / a paid Figma plan. Any rejection is
  surfaced in the UI when you create the deck.

## How it works

| Piece | File | Job |
|---|---|---|
| Figma API | `backend/figma.py` | Parse the URL, list top-level frames, fetch node trees, render images |
| Native renderer | `backend/pptx_native.py` | Rebuild each frame as editable PowerPoint shapes |
| App + scheduler | `backend/main.py` | REST API, Figma webhook receiver (live sync), daily safety-net job |
| Storage | `backend/store.py` | SQLite — one row per deck |
| UI | `frontend/index.html` | Single clean page |

### What converts to editable PowerPoint

- **Text** → native text boxes, with per-character runs (a bold/colored word
  inside a layer stays styled), font, size, color, alignment, line spacing.
- **Rectangles / rounded rects / ellipses / lines** → native autoshapes
  (fill, border, corner radius, rotation, transparency).
- **Frame background** → slide background.
- **Icons, custom vectors, gradients, image fills** → rendered as an image of
  just that node, pinned in place.

### Known limits (by design, for now)

- **Fonts** carry by name; they render exactly only if installed on the viewer's
  machine, otherwise PowerPoint substitutes.
- Auto-layout / constraints become **fixed positions**.
- **Drop shadows, masks, blend modes** are not yet mapped (shadows omitted
  deliberately — the XML is strict and a mistake makes PowerPoint show a repair
  dialog; safe to add as a follow-up).
- Tokens are stored in plain local SQLite — fine for single-user local use;
  productizing needs encryption + Figma OAuth.

## Getting the deck (one file, not a pile of downloads)

Each deck maps to **one** file on disk that's **overwritten** on every rebuild, so
you never accumulate `deck (1)`, `deck (2)`… Two ways to use it:

- **Open / Reveal** (buttons on each deck) — opens that single file in PowerPoint
  (or reveals it in Finder). After a rebuild, **close and reopen** to see changes.
  *Desktop PowerPoint can't live-refresh an already-open file — that's an app
  limitation, not the converter.*
- **Download** — a one-off copy (e.g. to email/share).

## Cloud sync — live updates in PowerPoint for the web (OneDrive/SharePoint)

> **Status: PARKED.** The implementation lives in `backend/cloud.py` but is
> currently **unwired** from the app (blocked by Microsoft corp-tenant admin
> consent for Graph scopes). To re-enable: re-add `import cloud`, the
> `cloud.sync_deck()` call in `main.generate`, the `/api/ms/*` + `/auth/ms/*`
> endpoints, and the UI connect bar. Test it against a personal Microsoft account
> or an M365 developer tenant (`MS_TENANT=common`) where you can self-consent.

When connected to a Microsoft account, every rebuild also uploads the deck to
OneDrive and gives you an **"Open in PowerPoint (web)"** link. Opened there, the
deck **auto-refreshes while open** — ideal for sharing org-wide.

### One-time Azure setup (only your org admin can do this)

1. **Azure Portal → App registrations → New registration.**
   - Name: `Figma Deck Sync`
   - Supported accounts: *single tenant* for your org (or *multitenant* to share org-wide)
   - Redirect URI: **Web** → `http://localhost:8000/auth/ms/callback`
     (add your hosted URL too once deployed)
2. Copy the **Application (client) ID** → this is `MS_CLIENT_ID`.
3. **Certificates & secrets → New client secret** → copy the value → `MS_CLIENT_SECRET`.
4. **API permissions → Microsoft Graph → Delegated**: add `User.Read`,
   `Files.ReadWrite`, `offline_access`, then **Grant admin consent**.
5. Run the app with those values set:
   ```bash
   MS_CLIENT_ID=xxxx MS_CLIENT_SECRET=yyyy ./run.sh
   ```
   (Optional: `MS_TENANT=<your-tenant-id>` to lock to your org; default `common`.)
6. Open the app → **Connect Microsoft account** → sign in. New rebuilds now sync,
   and each deck shows an *Open in PowerPoint (web)* link.

**Honest caveats:** wholesale-replacing a file that's *currently* open in the web
app can occasionally lag a beat or prompt; reopening always shows the latest.
Per-user org-wide rollout (everyone signs in with their own account) is the next
step beyond this single-account MVP.

## Next steps if productizing

Multi-user Figma OAuth · token encryption · delivery (email / Drive / SharePoint /
Teams) · per-deck custom schedules · webhook-driven refresh on file change ·
drop-shadow + rotation-group fidelity.
