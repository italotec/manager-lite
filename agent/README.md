# Card Client — Manager Lite local agent

Local GUI client that adds credit cards to WhatsApp Business Accounts (WABAs)
via Facebook billing, and opens AdsPower profiles on request. It runs next to
**AdsPower** on your PC and connects to your Manager Lite server, which
dispatches `add_card` jobs from the **Cartões** bulk-add modal, and
`open_browser` requests from the dashboard's "Abrir no AdsPower" button.

Card-adding drives a real, logged-in Facebook browser session (AdsPower
profile) via Playwright over CDP — it is not a public Graph API call, so this
local piece is required; Manager Lite (server) cannot do it alone.

## Setup

1. Install Python 3.11+ and [AdsPower](https://www.adspower.com/) (running
   locally, default `http://local.adspower.net:50325`).
2. From this `agent/` folder:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
3. (Optional) copy `.env.example` to `.env` and fill in `SERVER_URL` /
   `API_KEY` to pre-fill the GUI fields. Otherwise just paste them into the
   app each time.

## Run

```
python agent_gui.py
```

- **Server URL**: pre-filled with `https://manager-lite.verifywaba.store` (edit if you run a
  different instance).
- **API Key**: Manager Lite → API page (the same key used for the REST API).

Click **Conectar**. The status dot turns green when connected. Leave the app
running while you use the "Adicionar Cartão" bulk action or the "Abrir no
AdsPower" button on the dashboard.

Each WABA you add a card to must have `adspower_profile_id` set (Manager Lite →
Dashboard → edit WABA) so the agent knows which AdsPower profile to open.
`business_manager_id` is optional — if it's blank, the agent resolves the
owning Business Manager live from the logged-in Facebook session before
adding the card.

## WebSocket protocol (for reference)

Connect: `GET wss://<server>/agent/ws?token=<Manager Lite API key>`

Server → client:
```json
{"type": "add_card", "cmd_id": "<uuid>", "profile_id": "<adspower id>",
 "business_id": "<bm id>", "waba_id": "<waba id>",
 "card": {"number": "...", "exp_month": "...", "exp_year": "...", "csc": "...", "name": "..."}}
```
```json
{"type": "open_browser", "profile_id": "<adspower id>", "cmd_id": null}
```
and keepalive `{"type": "ping"}`.

Client → server:
```json
{"type": "card_result", "cmd_id": "<same>", "ok": true, "code": null,
 "credential_id": "...", "stage": "attach", "error": ""}
```
`open_browser` has no reply frame (fire-and-forget) — the resulting open profile shows up in the
next `browser_status` ping, sent every 5s:
```json
{"type": "browser_status", "open_profile_ids": [...]}
```

## Build to .exe

```
pyinstaller "Card Client.spec"
```
Produces `dist/Card Client/Card Client.exe` (or a single file, depending on
the spec). Never edit generated `build/`/`dist/` output by hand — re-run
PyInstaller after any source change.
