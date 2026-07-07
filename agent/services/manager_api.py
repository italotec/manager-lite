# -*- coding: utf-8 -*-
"""Client for Manager Lite's own /api/v1/business-managers endpoint.

Ported from I:\\Verificador Interface\\services\\manager_api.py — same
contract, since Manager Lite's POST /api/v1/business-managers
(app/routes/api.py) is itself an identical idempotent upsert + webhook
subscribe.
"""
import requests


def register_business_manager(
    base_url: str,
    api_key: str,
    waba_id: str,
    token: str,
    adspower_profile_id: str | None = None,
    serial_number: str | None = None,
    timeout: int = 15,
) -> dict:
    """POST /api/v1/business-managers — idempotent upsert + webhook subscribe.

    Returns:
        {
            "ok": bool,
            "status": int,
            "adspower_profile_id": str | None,
            "webhook_subscribed": bool | None,
            "webhook_error": str | None,
            "error": str | None,
        }
    """
    url = f"{base_url.rstrip('/')}/api/v1/business-managers"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body_payload: dict = {"waba_id": waba_id, "token": token}
    if adspower_profile_id:
        body_payload["adspower_profile_id"] = adspower_profile_id
    if serial_number:
        body_payload["serial_number"] = str(serial_number)
    try:
        resp = requests.post(
            url,
            json=body_payload,
            headers=headers,
            timeout=timeout,
        )
        try:
            body = resp.json()
        except Exception:
            body = {}

        if resp.status_code in (200, 201):
            return {
                "ok": body.get("ok", True),
                "status": resp.status_code,
                "adspower_profile_id": body.get("adspower_profile_id"),
                "webhook_subscribed": body.get("webhook_subscribed"),
                "webhook_error": body.get("webhook_error"),
                "error": None if body.get("ok", True) else body.get("error"),
            }

        return {
            "ok": False,
            "status": resp.status_code,
            "webhook_subscribed": None,
            "webhook_error": None,
            "error": body.get("error") or f"HTTP {resp.status_code}",
        }

    except requests.exceptions.Timeout:
        return {"ok": False, "status": 0, "webhook_subscribed": None, "webhook_error": None, "error": "timeout"}
    except Exception as exc:
        return {"ok": False, "status": 0, "webhook_subscribed": None, "webhook_error": None, "error": str(exc)}
