# -*- coding: utf-8 -*-
"""
facebook_scan.py — scan a logged-in, CDP-attached Playwright `page` for WABA
health (approved / disabled-appealable / disabled-in_review / permanently
disabled / restricted) across every Business Manager a profile can access,
plus profile-level checkpoint and not-logged-in detection.

Every signal here is network/URL based — never parsed from visible page
text — so it works regardless of the profile's UI language. All GraphQL
doc_ids/fields were confirmed live against real AdsPower profiles (see the
Manager Lite scanner plan). Reuses the token-minting pattern from
facebook_link.py.

Entry points:
    is_logged_in(page) -> bool
    get_uid(page) -> str
    is_checkpoint_url(url) -> bool
    is_login_url(url) -> bool
    enumerate_business_ids(page) -> list[dict]        # [{"id","name"}, ...]
    list_all_waba_assets(page, business_id) -> list[dict]  # [{"id","name"}, ...]
    assess_waba(page, business_id, waba_id) -> dict
    appeal_waba(page, business_id, waba_id, uid, ban_strike_id) -> bool
"""
from __future__ import annotations

import json
import re


# ── Shared GraphQL call (token harvest + POST /api/graphql/) ──────────────────

_GQL_JS = """
async (args) => {
    try {
        let dtsg = "", lsd = "", uid = "";
        try { dtsg = require("DTSGInitialData").token; } catch(_) {}
        try { lsd  = require("LSD").token; } catch(_) {}
        try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}
        if (!dtsg) { const i = document.querySelector('input[name="fb_dtsg"]'); if(i) dtsg = i.value; }
        if (!dtsg || !lsd) {
            const html = document.documentElement.innerHTML;
            if (!dtsg) { const m = html.match(/"DTSGInitialData",\\[\\],\\{"token":"([^"]+)"/); if(m) dtsg=m[1]; }
            if (!lsd)  { const m = html.match(/"LSD",\\[\\],\\{"token":"([^"]+)"/);            if(m) lsd=m[1];  }
        }
        if (!uid) { const m = document.cookie.match(/c_user=(\\d+)/); if(m) uid=m[1]; }
        if (!dtsg || !lsd || !uid) return {ok:false, err:"tokens missing dtsg="+!!dtsg+" lsd="+!!lsd+" uid="+!!uid};
        const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
        const jazoest = "2"+(charSum+50);
        const body = new URLSearchParams({
            av:uid, __user:uid, __a:"1", fb_dtsg:dtsg, jazoest, lsd, __comet_req:"15",
            fb_api_caller_class:"RelayModern",
            fb_api_req_friendly_name:args.friendlyName,
            server_timestamps:"true", variables:args.variables, doc_id:args.docId
        });
        const r = await fetch("/api/graphql/", {
            method:"POST",
            headers:{"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                     "x-fb-friendly-name":args.friendlyName},
            body: body.toString()
        });
        const txt = await r.text();
        let p = {}; try { p = JSON.parse(txt); } catch(_) {}
        return {ok: !p.errors, status:r.status, data: p.data || null, errors: p.errors || null};
    } catch(e) { return {ok:false, err:e.toString()}; }
}
"""


def _gql_call(page, friendly_name: str, doc_id: str, variables: dict, log=print) -> dict:
    try:
        result = page.evaluate(_GQL_JS, {
            "friendlyName": friendly_name,
            "docId": doc_id,
            "variables": json.dumps(variables, separators=(",", ":")),
        })
        if not result.get("ok"):
            log(f"[SCAN] {friendly_name} failed: {result}")
        return result
    except Exception as exc:
        log(f"[SCAN] {friendly_name} exception: {exc}")
        return {"ok": False, "err": str(exc)}


# ── Profile-level checks ───────────────────────────────────────────────────────

def is_logged_in(page, log=print) -> bool:
    """True if the `c_user` cookie is present (same regex every token-harvest
    helper in facebook_link.py already uses)."""
    try:
        return bool(page.evaluate("() => /c_user=(\\d+)/.test(document.cookie)"))
    except Exception as exc:
        log(f"[SCAN] is_logged_in check failed: {exc}")
        return False


def get_uid(page, log=print) -> str:
    try:
        return page.evaluate("() => (document.cookie.match(/c_user=(\\d+)/) || [])[1] || ''") or ""
    except Exception as exc:
        log(f"[SCAN] get_uid failed: {exc}")
        return ""


def is_checkpoint_url(url: str) -> bool:
    return "checkpoint" in (url or "")


def is_login_url(url: str) -> bool:
    return "login" in (url or "")


# ── Business Manager / WABA enumeration ────────────────────────────────────────

def enumerate_business_ids(page, log=print) -> list[dict]:
    """Return every Business Manager this profile can access, as [{"id","name"}, ...].

    Scrapes /select's anchors (the BM picker) — confirmed live against a
    3-BM profile. Falls back to the whatsapp_account redirect's business_id
    if /select yields nothing (single-BM profiles sometimes skip the picker).
    `name` is raw anchor text for display only — never used for detection.
    """
    results: dict[str, str] = {}
    try:
        page.goto("https://business.facebook.com/select", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
        anchors = page.evaluate(
            "() => Array.from(document.querySelectorAll(\"a[href*='business_id=']\")).map(a => "
            "({href: a.getAttribute('href'), text: (a.textContent || '').trim()}))"
        ) or []
        for a in anchors:
            m = re.search(r"business_id=(\d+)", a.get("href") or "")
            if m:
                results.setdefault(m.group(1), a.get("text") or "")
    except Exception as exc:
        log(f"[SCAN] enumerate_business_ids error: {exc}")

    if not results:
        try:
            page.goto("https://business.facebook.com/latest/settings/whatsapp_account",
                       wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)
            m = re.search(r"business_id[=/](\d+)", page.url)
            if m:
                results.setdefault(m.group(1), "")
        except Exception as exc:
            log(f"[SCAN] enumerate_business_ids fallback error: {exc}")

    return [{"id": bid, "name": name} for bid, name in results.items()]


def list_all_waba_assets(page, business_id: str, log=print) -> list[dict]:
    """Return every WABA asset in a BM, including disabled ones, as [{"id","name"}, ...].

    Clone of facebook_link.list_waba_ids_graphql with the status filter
    widened to null — that filter (["ACTIVE","ONBOARDING"]) silently drops
    disabled WABAs, confirmed live during Phase-0 research.
    """
    variables = {
        "businessID": business_id,
        "assetTypes": ["WHATSAPP_BUSINESS_ACCOUNT"],
        "searchTerm": None,
        "orderBy": None,
        "assetFilters": {
            "whatsapp_business_account_statuses": None,
            "exclude_catalog_segments": None,
        },
        "globalFilters": {},
        "shouldSkip": False,
        "shouldCountAdmin": False,
        "count": 25,
    }
    result = _gql_call(page, "BizKitSettingsBusinessAssetsListContainerQuery",
                        "27483473261255388", variables, log=log)
    if not result.get("ok"):
        return []

    seen: dict[str, str] = {}

    def walk(o):
        if isinstance(o, list):
            for item in o:
                walk(item)
            return
        if not isinstance(o, dict):
            return
        is_waba = (o.get("assetType") == "WHATSAPP_BUSINESS_ACCOUNT" or
                   o.get("business_asset_type") == "WHATSAPP_BUSINESS_ACCOUNT")
        wid = o.get("assetID") or o.get("business_object_id") or o.get("id")
        name = o.get("business_object_name") or o.get("wabaName") or ""
        if is_waba and wid:
            wid = str(wid)
            # The asset shows up in multiple shapes in this response (a bare
            # "node" with no name, plus a richer nested "business_object"
            # with the actual name) — keep the first non-empty name seen
            # instead of locking in an empty one from whichever shape walk()
            # reaches first.
            if name and not seen.get(wid):
                seen[wid] = name
            else:
                seen.setdefault(wid, "")
        for v in o.values():
            walk(v)

    walk(result.get("data") or {})
    return [{"id": wid, "name": name} for wid, name in seen.items()]


# ── Per-WABA status + appeal ────────────────────────────────────────────────────

def assess_waba(page, business_id: str, waba_id: str, log=print) -> dict:
    """Return {state, account_review_status, appeal_status, ban_strike_id, violations}.

    state ∈ approved|appealable|in_review|permanent|restricted|error — derived
    from account_review_status and the appeal_status of the strike responsible
    for the WABA-level ban (NOT the individual integrity_violations entries,
    which are informational only — confirmed live in Phase-0 research).
    """
    variables = {
        "assetId": waba_id,
        "templateStatuses": ["REJECTED"],
        "templateHasBeenAppealed": None,
        "templateRejectionReasonsToIgnore": ["CATEGORY_NOT_AVAILABLE"],
        "templateSpecificRejectionReasonsToIgnore": ["AUTH_LEGACY_DEPRECATION"],
        "migrationStatuses": ["NOTIFIED"],
    }
    result = _gql_call(page, "AccountQualityWhatsAppAccountViewWrapperQuery",
                        "26782016014733469", variables, log=log)
    if not result.get("ok"):
        return {"state": "error", "detail": result.get("err") or "graphql error"}

    w = (result.get("data") or {}).get("whatsAppAccountData") or {}
    name = w.get("name") or ""
    review_status = w.get("account_review_status")
    violations = w.get("integrity_violations") or []
    end_client = w.get("end_client_business_for_graphql") or {}
    strike = end_client.get("strike_responsible_for_whatsapp_bm_ban") or {}
    appeal_status = strike.get("appeal_status")
    ban_strike_id = strike.get("strike_id")

    if review_status == "PASSED":
        state = "approved"
    elif review_status == "BANNED":
        if appeal_status is None:
            state = "appealable"
        elif appeal_status == "IN_APPEAL":
            state = "in_review"
        elif appeal_status == "REJECTED":
            state = "permanent"
        else:
            state = "restricted"
    else:
        state = "restricted"

    return {
        "state": state,
        "name": name,
        "account_review_status": review_status,
        "appeal_status": appeal_status,
        "ban_strike_id": ban_strike_id,
        "violations": violations,
    }


def appeal_waba(page, business_id: str, waba_id: str, uid: str, ban_strike_id: str, log=print) -> bool:
    """Submit the WABA-ban appeal. Returns True on confirmed success.

    Visits the support-home page first to match the real user flow (the
    mutation is a plain fetch and doesn't strictly require it, but this was
    the live-confirmed sequence during Phase-0 research).
    """
    try:
        page.goto(f"https://business.facebook.com/business-support-home/{business_id}/{waba_id}/",
                   wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
    except Exception as exc:
        log(f"[SCAN] appeal_waba navigate failed: {exc}")

    variables = {
        "input": {
            "actor_id": uid,
            "client_mutation_id": "1",
            "violation_ids": [ban_strike_id],
            "entity_id": business_id,
            "appeal_comment": "",
            "callsite": "ACCOUNT_QUALITY_WHAT_YOU_CAN_DO",
        }
    }
    result = _gql_call(page, "useWhatsAppViolationAppealCreationMutation",
                        "24862954713329333", variables, log=log)
    if not result.get("ok"):
        return False
    payload = ((result.get("data") or {}).get("xfb_whats_app_violation_appeal_create") or {}).get(
        "violation_appeal_response_payload") or []
    return any(p.get("success") for p in payload)
