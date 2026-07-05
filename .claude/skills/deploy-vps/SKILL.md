---
name: deploy-vps
description: Commit local changes, push to origin/master on GitHub, then redeploy the Flask app on the production VPS via the ssh-manager MCP (git pull + `systemctl restart manager-lite`). Use when the user says "deploy", "deploy to vps", "update vps", "push and deploy", or anything equivalent.
---

# Deploy to VPS

Two-stage deployment for the Manager Lite Flask app: GitHub push + VPS restart.

| Field | Value |
|---|---|
| GitHub remote | `https://github.com/italotec/manager-lite.git` (owner=`italotec`, repo=`manager-lite`) |
| Branch | `master` |
| SSH alias | `myvps` (configured via `SSH_SERVER_MYVPS_*` env vars in this project's `.env` — scoped to Manager Lite, not the sibling Gerenciador de BMS repo) |
| Remote path | `/var/www/manager-lite` |
| App port | `5012` |
| Start cmd | **systemd service `manager-lite`** — `sudo systemctl restart manager-lite` (`Restart=always`, `RestartSec=3`, `MemoryHigh=2500M`, `MemoryMax=3G`, runs as `User=root`) |
| Sudo needs password | **always** — `.git` and the running process are owned by `root`; there is no non-sudo path on this box (unlike the sibling Manager repo, which may run under `administrator`) |
| Sibling app on same box | `/var/www/manager` (Gerenciador de BMS, port 5003, systemd service `manager`) — do not touch it during a Lite deploy; `ps aux | grep run.py` will show both, disambiguate by `readlink /proc/<pid>/cwd` |

## Pre-flight (always)

1. Run `git status` locally. Inspect every modified/untracked file.
2. **Refuse to commit anything containing secrets.** Grep staged content for these patterns and abort if matched:
   - `github_pat_`, `ghp_`, `gho_`, `ghs_` (GitHub tokens)
   - `Bearer ` followed by long opaque strings
   - `SSH_SERVER_*PASSWORD`
   - `Kimura1020` (known VPS credential — defense in depth)
   - `ANTHROPIC_API_KEY=sk-`, `OPENAI_API_KEY=sk-`
   - `password = "` or `password: "` with non-empty values
3. Files that must **never** be committed in this repo (gitignored or skip-staged):
   - `.env`, `*.env`
   - `.mcp.json`
   - `instance/app.db`, `instance/app.db-shm`, `instance/app.db-wal`
   - `instance/users/`
   - `__pycache__/`, `*.pyc`
   - `*.jsonl`, `*.log`
4. Confirm with the user which files to commit if there's ambiguity (new route/template files: usually yes; config/scratch files: ask). If unrelated feature work is sitting uncommitted alongside the change you were asked to deploy, ask explicitly whether to bundle it or hold it back.

## Phase A — GitHub push

1. Stage allowed files **explicitly** (never `git add .`):
   ```
   git add <each-file-by-name>
   ```
2. Generate a commit message from the diff. Match the existing repo style — short Portuguese descriptions. Switch to conventional-commit prefixes (`feat:`, `fix:`, etc.) only if the user asks.
3. `git push origin master`. If rejected non-fast-forward, run `git pull --rebase origin master` then re-push.
4. Verify push with the GitHub MCP:
   ```
   mcp__github__get_commit(owner="italotec", repo="manager-lite", sha="master", include_diff=false)
   ```
   Compare the returned `sha` against local `git rev-parse HEAD` — both must match.

## Phase B — VPS redeploy (ssh-manager MCP)

### B0. Probe ownership and branch

Run this **once per deploy session**, then remember the result for B1–B5. On this box the answer has consistently been "sudo, always" — but re-probe if anything looks off:

```
ssh_execute(
  server="myvps",
  command="ls -ld /var/www/manager-lite && stat -c '%U %G' /var/www/manager-lite/.git"
)
```
(expect `.git` owned by `root` → use `ssh_execute_sudo` for every remaining step)

```
ssh_execute_sudo(
  server="myvps",
  command='bash -c "git config --global --add safe.directory /var/www/manager-lite; git -C /var/www/manager-lite symbolic-ref --short HEAD"',
  password=<SSH_SERVER_MYVPS_PASSWORD from .env>
)
```
- Anything other than `master` → run `git -C /var/www/manager-lite fetch origin && git -C /var/www/manager-lite checkout master && git -C /var/www/manager-lite pull origin master` before proceeding to B1.

### B1. Pull latest code

```
ssh_execute_sudo(
  server="myvps",
  command='bash -c "cd /var/www/manager-lite && git pull origin master"',
  password=<from .env>
)
```

**Handle "could not resolve host: github.com"**: retry once — transient DNS on the VPS.

**Handle "Your local changes would be overwritten"**: stash first:
```
bash -c "cd /var/www/manager-lite && git stash push -m pre-deploy-stash && git pull origin master && git stash pop"
```

If `git stash pop` reports conflicts, **stop and show the user the conflict markers**. Do not auto-resolve. Then:
- Keep upstream version: `git checkout HEAD -- <path>`
- Re-apply a specific local edit: use `sed -i` after restoring HEAD
- Finish: `git stash drop`

### B2. Install/upgrade Python dependencies

Run after **every** pull — a missing dep surfaces as `ModuleNotFoundError` at the first request, not at deploy time. This box uses a venv at `/var/www/manager-lite/.venv`:

```
ssh_execute_sudo(
  server="myvps",
  command='bash -c "/var/www/manager-lite/.venv/bin/python -m pip install -r /var/www/manager-lite/requirements.txt"',
  password=<from .env>
)
```

The install is idempotent; already-satisfied packages are skipped.

> **Python version trap:** the VPS interpreter version may differ from local dev. Syntax legal locally can `SyntaxError` in production. After pulling, optionally smoke-test:
> ```
> bash -c "cd /var/www/manager-lite && /var/www/manager-lite/.venv/bin/python -c 'import app, app.routes.wabas, app.routes.api, app.services.sync_service, app.services.meta'"
> ```

### B3 + B4. Restart the systemd service

The app runs as the **systemd service `manager-lite`** (`/etc/systemd/system/manager-lite.service`,
`User=root`, `Restart=always`, `MemoryMax=3G`). A single restart replaces the old kill-port + start-screen
dance — systemd stops the old process (and its whole cgroup) and starts the new one atomically:

```
ssh_execute_sudo(
  server="myvps",
  command="systemctl restart manager-lite",
  password=<from .env>
)
```

> **Do NOT start a `screen` or run `python run.py` manually.** That would bind a second
> process and fight the systemd instance. The service owns the app now. Note the sibling
> `manager` service (port 5003, `/var/www/manager`) runs its own `run.py` — don't confuse
> the two when grepping `ps aux`.

### B5. Verify deploy

```
ssh_execute_sudo(
  server="myvps",
  command='bash -c "sleep 5 && systemctl is-active manager-lite && echo --- && ss -ltnp | grep 5012 && echo --- && systemctl status manager-lite --no-pager -n 5"',
  password=<from .env>
)
```

Expect:
- `systemctl is-active manager-lite` → `active`
- One `python` row LISTEN on `0.0.0.0:5012`
- `status` shows `active (running)` and a low `Memory:` figure

If the service is `failed` / not listening, read the boot traceback:
```
ssh_execute_sudo(server="myvps", command="journalctl -u manager-lite --no-pager -n 60", password=<from .env>)
# or inline run to see the SyntaxError/ImportError directly:
ssh_execute_sudo(server="myvps", command='bash -c "cd /var/www/manager-lite && timeout 10 .venv/bin/python run.py 2>&1 | head -60"', password=<from .env>)
```

Live app log: `/var/log/manager-lite.log` (stdout+stderr, includes Flask request log).

## Failure modes / recovery

| Symptom | Cause | Fix |
|---|---|---|
| `git push` rejected (non-fast-forward) | Someone pushed since last fetch | `git pull --rebase origin master` then re-push |
| `Could not resolve host: github.com` on VPS | Transient DNS | Retry. `cat /etc/resolv.conf` if persistent. |
| `Permission denied` on `.git/` | `.git/` owned by root | Use `ssh_execute_sudo` not `ssh_execute` (this is always the case on this box) |
| `sudo cd: command not found` | MCP prepends `sudo` to the literal command | Wrap in `bash -c "..."` |
| Stash pop conflicts | VPS had uncommitted edits overlapping pushed code | Stop, show user, resolve per-file with `git checkout HEAD -- <path>` |
| `manager-lite.service` is `failed` after restart | App crashed on boot (SyntaxError/ImportError) | `journalctl -u manager-lite -n 60`, or run `.venv/bin/python run.py` inline (see B5) |
| `ModuleNotFoundError` on first request after deploy | B2 didn't run, or ran against the wrong interpreter | Re-run B2 against `/var/www/manager-lite/.venv/bin/python` |
| Port 5012 bound by a stray process (not systemd) | Someone started a manual `python run.py` | `systemctl stop manager-lite`, `kill` the stray PID from `ss -ltnp | grep 5012` (confirm cwd via `/proc/<pid>/cwd` first — don't kill the sibling `manager` app's process by mistake), then `systemctl start manager-lite` |

## End-of-deploy summary to user

Report back:
- Local commit SHA pushed (`git rev-parse HEAD`)
- GitHub commit URL: `https://github.com/italotec/manager-lite/commit/<sha>`
- VPS HEAD (`git -C /var/www/manager-lite rev-parse HEAD`) — confirm it matches
- `systemctl is-active manager-lite` + PID/listen address from `ss -ltnp | grep 5012`
- Any unresolved working-tree drift on the VPS (file list)
