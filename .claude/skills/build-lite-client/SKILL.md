---
name: build-lite-client
description: Build a fresh Lite Client.exe (Windows) or Lite Client.app (macOS) from the current agent/ source, run staleness/spec/config gates, smoke-test the log output, archive the old binary, and record the build hash. Use when the user says "build the agent", "build the client", "rebuild Lite Client", "ship a new agent build", or when agent/ has changed and a stale binary needs replacing (e.g. Verificar's Nº/serial column showing blank).
---

# /build-lite-client

Build a fresh `Lite Client.exe` (Windows) or `Lite Client.app` (macOS) from the
current working tree, verify it passes all gates, and archive the old binary.
**Never build by hand** — always run these gates so a stale or broken build
never ships. This mirrors `build-verificador-agent` from the Verificador
Interface repo, adapted for this repo's layout (no `agent.py`/`agent_gui.py`
duplication to guard against, no `WORKER_API_KEY`).

---

## When to run

- After any change under `agent/` (`agent_core.py`, `agent_gui.py`,
  `config.py`, `services/*.py`, `Lite Client.spec`, `Lite Client-mac.spec`).
- Before distributing a binary to any operator machine.
- Anytime `/verificar` shows stale data that a newer agent would fix (e.g. a
  blank `Nº` serial column) — that is a symptom of exactly this problem.

---

## Step-by-step procedure

### Gate 1 — Working tree freshness

```powershell
git status --short
git log -1 --format="%h %ci %s" -- agent/
git rev-parse --short HEAD
```

If `git status` shows unstaged changes under `agent/`, stop and ask the user
whether to commit or stash first. Record the short hash — it goes into
`build_history.txt` (Gate 8) and is what Gate 2 compares against next time.

### Gate 2 — Staleness check (the gate that catches silent drift)

`agent_core.py`, `config.py`, and `services/` are bundled as PyInstaller
**datas**, not analyzed/compiled in — the shipped copy inside a `.app` bundle
is a literal file copy of the source at build time. That makes it directly
diffable:

```powershell
git diff --no-index --stat "agent/agent_core.py" "agent/dist/Lite Client.app/Contents/Resources/agent_core.py"
```

Any output ⇒ the existing macOS bundle is stale, rebuild. No output ⇒ current.

The Windows build is a packed onefile exe and can't be diffed this way —
instead compare the current `git rev-parse --short HEAD` against the last
line of `agent/dist/build_history.txt`. If they differ (or the file/binary is
missing), the Windows exe is stale.

**Why this matters:** this exact gap (agent_core.py changed in commit
7febf3a, shipped exe/`.app` never rebuilt) is what caused the Verificar page's
serial-number column to render blank for every profile — the server-side code
was correct throughout; only the binary lagged.

### Gate 3 — Spec completeness

Confirm `Lite Client.spec` (Windows) and `Lite Client-mac.spec` (macOS) both
declare, in `datas`: `services`, `agent_core.py`, `config.py`,
`prosperidadelogo.ico`; and in `_collect`: `playwright`, `requests`,
`certifi`, `dotenv`.

```powershell
$spec = Get-Content "agent\Lite Client.spec" -Raw
foreach ($item in @("services","agent_core.py","config.py","prosperidadelogo.ico",
                     "playwright","requests","certifi","dotenv")) {
    if ($spec -notmatch [regex]::Escape($item)) { Write-Host "MISSING: $item" }
}
```

Then confirm every `services/*.py` module has a matching `hiddenimports`
entry — since `services/` ships as data, PyInstaller never statically
analyzes its imports, so a lib a service lazily imports must be collected
explicitly or the frozen build only fails at runtime, on whichever code path
uses it:

```powershell
Get-ChildItem "agent\services\*.py" | Where-Object { $_.Name -ne "__init__.py" } | ForEach-Object {
    $mod = "services.$($_.BaseName)"
    if ($spec -notmatch [regex]::Escape($mod)) { Write-Host "MISSING hiddenimport: $mod" }
}
```

Fix the spec (both `.spec` files, kept in sync) before proceeding.

### Gate 4 — Config check

```powershell
Select-String "DEFAULT_SERVER_URL" agent\config.py
```

Confirm `DEFAULT_SERVER_URL = os.getenv("SERVER_URL", "https://manager-lite.verifywaba.store")`.
Confirm `DEFAULT_API_KEY` still defaults to `""` — it must **never** be
hardcoded, since operators paste their own key and a baked-in default would
ship a live credential in the binary.

### Gate 5 — Build (platform branch)

**Windows (local):**

```powershell
$old = "agent\dist\Lite Client.exe"
if (Test-Path $old) {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    Move-Item $old "agent\dist\Lite Client_OLD_$ts.exe"
}
py -m PyInstaller --noconfirm --clean "agent\Lite Client.spec" *> agent\build_exe.log
Get-Content agent\build_exe.log -Tail 10
```

Must end with `Building EXE from EXE-00.toc completed successfully.` Any
`No module named X` → add `X` to `hiddenimports` in `Lite Client.spec`,
rebuild.

**macOS:** PyInstaller cannot cross-compile — never attempt this on Windows.
Either trigger CI (`gh workflow run build-mac-client.yml` in this repo, then
`gh run download` the `Lite-Client-macOS` artifact) or build on an actual Mac
per [agent/README.md](../../../agent/README.md) ("Build for macOS"), which
covers the `.icns` generation and `pyinstaller "Lite Client-mac.spec"` step.

Whichever platform you just built, note that the other one is now unverified
against this commit — don't assume both are current together.

### Gate 6 — Artifact name assertion

The produced binary must be exactly `agent/dist/Lite Client.exe` or
`agent/dist/Lite Client.app` — check the spec's `name=` fields if it isn't.
**Never hand-rename the output.** (This exact drift — a `Card Client` spec
producing a hand-renamed `Lite Client.exe` — is why the specs and this skill
exist under the `Lite Client` name now: source and artifact must match.)

### Gate 7 — Smoke test (mandatory — do not skip)

Run the fresh binary against one real agent connection with AdsPower running.
Logs land next to the binary automatically:
`agent/dist/logs/agent_YYYYMMDD_HHMMSS.log`.

```powershell
Get-Content "agent\dist\logs\$(Get-ChildItem 'agent\dist\logs' -Filter agent_*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty Name)" -Wait
```

**The log must show, in order, before the build ships:**

| Marker | Source | Proves |
|---|---|---|
| `[AGENT] Conectado!` | `agent_core.py` `_run_agent` | WS auth + connect succeeded |
| `[SYNC] N perfis do grupo Verificar enviados` | `agent_core.py` `_sync_profiles` | `profiles_push` ran — **N > 0** confirms the serial-number sync path works |
| `[CMD] Browser aberto para <pid>` | `agent_core.py` `_handle_open_browser` | `open_browser` handled — this is what lights the green dot on `/verificar` and the dashboard |

If also testing the Conectar flow, additionally look for
`[LINK <pid>] Resumo: ok=…` (link summary, end-to-end).

If any marker is absent, investigate with the log before distributing —
never ship on a partial smoke test.

**Task-specific acceptance check:** with the fresh build connected, open
`/verificar` in the browser and confirm the `Nº` column shows real numbers
instead of `—`.

### Gate 8 — Embed build info

```powershell
$hash = git rev-parse --short HEAD
$ts   = Get-Date -Format "yyyy-MM-dd HH:mm"
Add-Content "agent\dist\build_history.txt" "$ts  $hash  $(git log -1 --format='%s')"
```

---

## Distributing the binary

- **Windows:** copy only `agent\dist\Lite Client.exe` to operator machines.
- **macOS:** copy the whole `agent/dist/Lite Client.app` bundle. If it's ever
  moved between Macs (not built directly on the target machine), clear the
  quarantine flag once: `xattr -dr com.apple.quarantine "Lite Client.app"`.

`dist/logs/` is created automatically next to the binary on first run — no
extra files needed. If an operator reports a hang or unexpected behavior, ask
for their `logs/agent_*.log` file — it contains the full trace.

---

## Rollback

```powershell
# List archived Windows builds
Get-ChildItem "agent\dist\Lite Client_OLD_*.exe"
# Restore a previous build
Copy-Item "agent\dist\Lite Client_OLD_YYYYMMDD_HHMMSS.exe" "agent\dist\Lite Client.exe"
```

For macOS, restore the last known-good `Lite Client.app` from wherever it was
archived (CI artifact history via `gh run list --workflow=build-mac-client.yml`,
or a manual backup) — there is no automatic archive step for `.app` bundles
since Gate 5's archive logic only covers the Windows onefile exe.
