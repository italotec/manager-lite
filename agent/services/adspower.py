"""
AdsPower local API client.
Handles profile CRUD, group management, and browser open/close.
"""
import json
import time
import threading
import requests

# Desktop Windows Chrome UA injected into every new profile so FB never sees mobile
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def normalize_cookie(cookie: str, domain: str = ".facebook.com") -> str:
    """Normalize a cookie string into AdsPower's expected JSON-array format.

    AdsPower's /user/create `cookie` field wants a JSON array of cookie
    objects.  Accepts either:
      - already-JSON (starts with `[` or `{`) → returned unchanged
      - header-style "name=value; name2=value2" → converted to JSON

    The header-style value is what users paste from a logged-in profile
    (e.g. "c_user=...;xs=...;fr=...;datr=...;").  Each pair becomes a cookie
    object scoped to *domain* so AdsPower applies it on the next browser open.
    """
    s = (cookie or "").strip()
    if not s:
        return ""
    # Already a JSON payload — pass through untouched.
    if s[0] in "[{":
        return s

    if not domain.startswith("."):
        domain = "." + domain.lstrip(".")

    items = []
    for pair in s.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")  # split on first '=' only
        name = name.strip()
        if not name:
            continue
        items.append({
            "name":     name,
            "value":    value.strip(),
            "domain":   domain,
            "path":     "/",
            "hostOnly": False,
            "httpOnly": False,
            "secure":   True,
            "sameSite": "no_restriction",
        })
    return json.dumps(items) if items else ""


class AdsPowerClient:
    _last_request_at: float = 0.0  # shared across all instances
    _throttle_lock = threading.Lock()

    def __init__(self, base_url: str = "http://local.adspower.net:50325"):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()

    # ── low-level ────────────────────────────────────────────────────────────

    def _throttle(self, min_interval: float = 1.1):
        """Ensure at least *min_interval* seconds between consecutive API calls, across all instances."""
        with AdsPowerClient._throttle_lock:
            elapsed = time.time() - AdsPowerClient._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            AdsPowerClient._last_request_at = time.time()

    def _get(self, path: str, **params):
        for attempt in range(3):
            self._throttle()
            try:
                r = self.session.get(f"{self.base}{path}", params=params, timeout=30)
            except requests.exceptions.ConnectionError:
                raise RuntimeError(
                    "AdsPower não está em execução. Abra o AdsPower e tente novamente."
                )
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0,):
                msg = data.get("msg", "")
                if "Too many" in msg and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError(f"AdsPower error [{path}]: {msg} | params={params}")
            return data.get("data", {})

    def _post(self, path: str, body: dict):
        for attempt in range(3):
            self._throttle()
            try:
                r = self.session.post(f"{self.base}{path}", json=body, timeout=30)
            except requests.exceptions.ConnectionError:
                raise RuntimeError(
                    "AdsPower não está em execução. Abra o AdsPower e tente novamente."
                )
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0,):
                msg = data.get("msg", "")
                if "Too many" in msg and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError(f"AdsPower error [{path}]: {msg}")
            return data.get("data", {})

    # ── groups ────────────────────────────────────────────────────────────────

    def get_group_id(self, group_name: str) -> str:
        """Return group_id for *group_name*, creating the group if it doesn't exist."""
        data = self._get("/api/v1/group/list", page=1, page_size=200)
        for g in data.get("list", []):
            if g["group_name"] == group_name:
                return str(g["group_id"])
        # Create it
        result = self._post("/api/v1/group/create", {"group_name": group_name})
        return str(result["group_id"])

    # ── profiles ──────────────────────────────────────────────────────────────

    def list_profiles(self, group_id: str = "", page_size: int = 50) -> list[dict]:
        """Return all profiles, optionally filtered by group_id."""
        profiles = []
        page = 1
        while True:
            params = {"page": page, "page_size": page_size}
            if group_id:
                params["group_id"] = group_id
            data = self._get("/api/v1/user/list", **params)
            batch = data.get("list", [])
            profiles.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return profiles

    def get_profile(self, user_id: str) -> dict:
        data = self._get("/api/v1/user/list", user_id=user_id)
        lst = data.get("list", [])
        if not lst:
            raise RuntimeError(f"Profile {user_id} not found")
        return lst[0]

    def create_profile(
        self,
        name: str,
        username: str = "",
        password: str = "",
        fakey: str = "",
        proxy_config: dict | None = None,
        group_id: str = "0",
        remark: str = "",
        platform: str = "facebook.com",
        cookie: str = "",
    ) -> str:
        """Create a new AdsPower profile. Returns the new user_id.

        When `cookie` is provided it is sent in the create payload so the
        profile is logged in atomically — more reliable than a follow-up
        /user/update, which can race against a freshly-created profile.
        """
        payload = {
            "name": name,
            "domain_name": platform,
            "username": username,
            "password": password,
            "fakey": fakey,
            "group_id": str(group_id),
            "remark": remark,
            "user_proxy_config": proxy_config or {"proxy_soft": "no_proxy"},
            # Force desktop fingerprint so Facebook never serves the mobile layout
            "fingerprint_config": {
                "ua": _DESKTOP_UA,
                "os": "windows",
                "browser": "chrome",
            },
        }
        if cookie:
            payload["cookie"] = normalize_cookie(cookie, "." + platform.lstrip("."))
        result = self._post("/api/v1/user/create", payload)
        return result["id"]

    def update_profile(self, user_id: str, **fields):
        """Update arbitrary profile fields (group_id, remark, username, etc.)."""
        body = {"user_id": user_id, **fields}
        self._post("/api/v1/user/update", body)

    def move_to_group(self, user_id: str, group_id: str = "0"):
        """Move profile to group_id (use '0' to remove from all groups)."""
        self.update_profile(user_id, group_id=str(group_id))

    def share_profiles(self, profile_ids: list[str], receiver: str, content: list[str] | None = None) -> str:
        body: dict = {"profile_id": profile_ids, "receiver": receiver, "share_type": 1}
        if content:
            body["content"] = content
        result = self._post("/api/v2/browser-profile/share", body)
        return result.get("group_name", "")

    def delete_profile(self, user_id: str) -> None:
        """Permanently delete a profile from AdsPower."""
        self._post("/api/v1/user/delete", {"user_ids": [user_id]})

    # ── browser ───────────────────────────────────────────────────────────────

    def open_browser(self, user_id: str, headless: bool = False) -> dict:
        """
        Start the profile browser.
        Returns dict with keys: ws (puppeteer, selenium), debug_port, webdriver.
        """
        params = {"user_id": user_id}
        if headless:
            params["headless"] = "1"
        return self._get("/api/v1/browser/start", **params)

    def close_browser(self, user_id: str):
        """Stop the profile browser (best-effort)."""
        try:
            self._get("/api/v1/browser/stop", user_id=user_id)
        except Exception:
            pass

    def is_browser_active(self, user_id: str) -> str:
        """Return 'active', 'inactive', or 'error'.

        'inactive' is only returned on an explicit Inactive signal from AdsPower.
        Any non-zero code, network error, missing endpoint, or unexpected payload
        returns 'error' — so transient AdsPower flakiness never falsely closes a
        profile dot on the dashboard.
        """
        try:
            self._throttle()
            r = self.session.get(
                f"{self.base}/api/v1/browser/active",
                params={"user_id": user_id},
                timeout=10,
            )
            if not r.ok:
                return "error"
            data = r.json()
            if data.get("code") != 0:
                return "error"
            status = (data.get("data") or {}).get("status", "")
            if status == "Active":
                return "active"
            if status == "Inactive":
                return "inactive"
            return "error"
        except Exception:
            return "error"


# ── module-level helpers ──────────────────────────────────────────────────────

def connect_cdp_with_retry(
    playwright,
    ws_url: str,
    *,
    profile_id: str | None = None,
    ads_client: "AdsPowerClient | None" = None,
    attempts: int = 3,
    timeout_ms: int = 60_000,
    backoff_s: float = 2.5,
):
    """Connect Playwright to an AdsPower-launched browser with retry.

    AdsPower returns the CDP WS URL the instant Chrome is forked, but the
    DevTools target dispatcher may not be ready for several seconds (extensions
    loading, cookies/local-storage hydrating).  The WS accepts the connection
    immediately but doesn't respond to Target.setAutoAttach — so Playwright's
    handshake times out even though <ws connected> appears in the log.

    Strategy:
    - Use 60 s per attempt instead of Playwright's 30 s default.
    - Retry up to `attempts` times with `backoff_s` sleep between them.
    - On last failure, if ads_client + profile_id are provided, recycle the
      profile (stop → start) to clear any stale CDP targets, then try once more.

    Returns (browser, ws_url_actually_used).
    """
    from playwright.sync_api import Error as PWError

    last_exc: Exception | None = None
    current_ws = ws_url

    for i in range(attempts):
        try:
            browser = playwright.chromium.connect_over_cdp(current_ws, timeout=timeout_ms)
            return browser, current_ws
        except PWError as e:
            last_exc = e
            print(f"[CDP] connect attempt {i + 1}/{attempts} failed: {e}")
            if i < attempts - 1:
                time.sleep(backoff_s)

    # Final attempt: recycle the AdsPower profile to clear stale targets
    if ads_client and profile_id:
        print(f"[CDP] recycling profile {profile_id} after {attempts} failed connects")
        try:
            ads_client.close_browser(profile_id)
        except Exception:
            pass
        time.sleep(2.0)
        fresh = ads_client.open_browser(profile_id)
        current_ws = fresh.get("ws", {}).get("puppeteer", "") or current_ws
        try:
            browser = playwright.chromium.connect_over_cdp(current_ws, timeout=timeout_ms)
            return browser, current_ws
        except PWError as e:
            last_exc = e

    raise last_exc
