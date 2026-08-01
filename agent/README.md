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

## Build to .exe (Windows)

```
pyinstaller "Card Client.spec"
```
Produces `dist/Card Client/Card Client.exe` (or a single file, depending on
the spec). Never edit generated `build/`/`dist/` output by hand — re-run
PyInstaller after any source change.

## Build for macOS

PyInstaller cannot cross-compile — this must run **on a Mac**. Playwright
attaches to AdsPower's already-running Chromium over CDP
(`services/adspower.py`), so `playwright install chromium` is **not** needed;
only the bundled Playwright node driver is.

Prerequisites (once, on the Mac):
- arm64 Python 3.11+ with Tk 8.6 (python.org installer, or Homebrew
  `python@3.12` + `brew install python-tk@3.12`). Avoid the macOS system
  Python (old Tk 8.5 breaks Tkinter). Verify with:
  ```
  python3 -c "import platform, tkinter; print(platform.machine(), tkinter.TkVersion)"
  ```
  Expect `arm64 8.6`.
- [AdsPower for macOS](https://www.adspower.com/) installed and running.

Build:
```
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

# one-time: generate prosperidadelogo.icns from the bundled 1024x1024 mark
# (agent/prosperidade_icon_1024.png — cropped from the ProsperidadePay logo,
# wordmark removed since it's illegible at small icon sizes)
mkdir icon.iconset
sips -z 16 16     prosperidade_icon_1024.png --out icon.iconset/icon_16x16.png
sips -z 32 32     prosperidade_icon_1024.png --out icon.iconset/icon_16x16@2x.png
sips -z 32 32     prosperidade_icon_1024.png --out icon.iconset/icon_32x32.png
sips -z 64 64     prosperidade_icon_1024.png --out icon.iconset/icon_32x32@2x.png
sips -z 128 128   prosperidade_icon_1024.png --out icon.iconset/icon_128x128.png
sips -z 256 256   prosperidade_icon_1024.png --out icon.iconset/icon_128x128@2x.png
sips -z 256 256   prosperidade_icon_1024.png --out icon.iconset/icon_256x256.png
sips -z 512 512   prosperidade_icon_1024.png --out icon.iconset/icon_256x256@2x.png
sips -z 512 512   prosperidade_icon_1024.png --out icon.iconset/icon_512x512.png
cp prosperidade_icon_1024.png icon.iconset/icon_512x512@2x.png
iconutil -c icns icon.iconset -o prosperidadelogo.icns

pyinstaller "Card Client-mac.spec"
```
Produces `dist/Card Client.app` (native arm64). Since this is a local,
unsigned build (no Apple Developer account / notarization), macOS won't
Gatekeeper-block it as long as it was built directly on that Mac rather than
downloaded/AirDropped. If it's ever moved between Macs, clear the quarantine
flag once: `xattr -dr com.apple.quarantine "Card Client.app"`.

Logs are written next to the app (inside `Card Client.app/Contents/MacOS/logs/`),
same convention as Windows — keep the `.app` in a user-writable folder
(Desktop/Downloads), not `/Applications`.
