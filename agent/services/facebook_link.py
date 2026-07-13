# -*- coding: utf-8 -*-
"""
facebook_link.py — share a WABA to a partner Business Manager over an
already-logged-in, CDP-attached Playwright `page`, and extract/resolve the
WABA id along the way.

Ported from I:\\Verificador Interface\\services\\facebook_bot.py (functions
_list_waba_ids_graphql / _extract_waba_id_graphql / _check_bm_restricted_graphql
/ _share_waba_graphql / _resolve_owning_business_id). Standalone here — no
FacebookBot/gerador/sms dependency, just a Playwright `page`.

Entry points:
    extract_waba_id_graphql(page, business_id, expected_name=None) -> str | None
    check_bm_restricted_graphql(page, business_id) -> bool | None
    share_waba_graphql(page, business_id, partner_business_id, waba_id) -> bool
    resolve_owning_business_id(page) -> str | None
"""
from __future__ import annotations

import json
import re


class BmRestrictedException(RuntimeError):
    """Raised when the Business Manager is detected as restricted/disabled for advertising."""
    pass


# ── WABA partner-sharing permission task IDs (full WhatsApp permission set) ──
WABA_FULL_PERMISSION_TASKS = [
    "1178671679425452", "2267064093541969", "468633770384254", "337342957148859",
    "1455369811941901", "3507194632893280", "934369393579850", "1292349377982198",
    "281909864313192",  "446593557821945",  "3863350093762893", "314336691716181",
    "507689495019576",  "617932692057625",
]


def list_waba_ids_graphql(page, business_id: str, log=print) -> list[dict]:
    """Return all WABA assets in a BM as [{"id": str, "name": str}, ...].

    Uses BizKitSettingsBusinessAssetsListContainerQuery (doc_id 27483473261255388).
    Returns an empty list on any error so callers can iterate safely.
    """
    variables = json.dumps({
        "businessID": business_id,
        "assetTypes": ["WHATSAPP_BUSINESS_ACCOUNT"],
        "searchTerm": None,
        "orderBy": None,
        "assetFilters": {
            "whatsapp_business_account_statuses": ["ACTIVE", "ONBOARDING"],
            "exclude_catalog_segments": None,
        },
        "globalFilters": {},
        "shouldSkip": False,
        "shouldCountAdmin": False,
        "count": 10,
    }, separators=(",", ":"))
    js = """
    async (vars) => {
        try {
            let dtsg = "", lsd = "", uid = "";
            try { dtsg = require("DTSGInitialData").token; } catch(_) {}
            try { lsd  = require("LSD").token;             } catch(_) {}
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
            const reqBody = new URLSearchParams({
                av:uid, __user:uid, __a:"1", fb_dtsg:dtsg, jazoest, lsd, __comet_req:"15",
                fb_api_caller_class:"RelayModern",
                fb_api_req_friendly_name:"BizKitSettingsBusinessAssetsListContainerQuery",
                server_timestamps:"true", variables:vars, doc_id:"27483473261255388"
            });
            const r = await fetch("/api/graphql/", {
                method:"POST",
                headers:{"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                         "x-fb-friendly-name":"BizKitSettingsBusinessAssetsListContainerQuery"},
                body:reqBody.toString()
            });
            const txt = await r.text();
            let p = {};
            try { p = JSON.parse(txt); } catch(_) {}

            const seen = {};
            const wabas = [];
            function walk(o) {
                if (!o || typeof o !== "object") return;
                if (Array.isArray(o)) { o.forEach(walk); return; }
                const isWaba = (o.assetType === "WHATSAPP_BUSINESS_ACCOUNT" ||
                                o.business_asset_type === "WHATSAPP_BUSINESS_ACCOUNT");
                const id = o.assetID || o.business_object_id || o.id;
                if (isWaba && id && !seen[id]) {
                    seen[id] = true;
                    wabas.push({ id: String(id), name: o.business_object_name || o.wabaName || "" });
                }
                Object.values(o).forEach(walk);
            }
            walk(p);

            return { ok: !p.errors && wabas.length > 0, wabas, status: r.status, body: txt.slice(0, 200) };
        } catch(e) { return {ok:false, err:e.toString(), wabas:[]}; }
    }
    """
    try:
        result = page.evaluate(js, variables)
        if not result.get("ok"):
            log(f"[WABA_LINK] list_waba_ids_graphql error for bm={business_id}: {result}")
            return []
        return result.get("wabas") or []
    except Exception as exc:
        log(f"[WABA_LINK] list_waba_ids_graphql exception for bm={business_id}: {exc}")
        return []


def extract_waba_id_graphql(page, business_id: str, expected_name: str | None = None, log=print) -> str | None:
    """Query the BM's WABA asset list and return the numeric WABA id.

    If expected_name is given, prefers the matching WABA; otherwise returns the first.
    """
    wabas = list_waba_ids_graphql(page, business_id, log=log)
    if not wabas:
        log(f"[WABA_LINK] no WABA assets found for business_id={business_id}")
        return None
    if expected_name:
        for w in wabas:
            if expected_name.lower() in (w.get("name") or "").lower():
                log(f"[WABA_LINK] matched waba_id={w['id']} name={w.get('name')!r}")
                return w["id"]
    waba_id = wabas[0]["id"]
    log(f"[WABA_LINK] extracted waba_id={waba_id} (first of {len(wabas)})")
    return waba_id


def check_bm_restricted_graphql(page, business_id: str, log=print) -> bool | None:
    """Probe BIActorBasicSpokeQuery (doc_id 24025468177059596) for restriction.

    Returns True if restricted, False if explicitly not restricted, None on
    any error (treat as unknown — don't mark as restricted on noise).
    """
    js = """
    async (entityId) => {
        try {
            let dtsg = "", lsd = "", uid = "";
            try { dtsg = require("DTSGInitialData").token; } catch(_) {}
            try { lsd  = require("LSD").token;             } catch(_) {}
            try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}
            if (!dtsg) { const i = document.querySelector('input[name="fb_dtsg"]'); if(i) dtsg=i.value; }
            if (!uid) { const m = document.cookie.match(/c_user=(\\d+)/); if(m) uid=m[1]; }
            if (!dtsg || !lsd || !uid) return {err:"tokens missing"};
            const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
            const jazoest = "2"+(charSum+50);
            const body = new URLSearchParams({
                av:uid, __user:uid, __a:"1", fb_dtsg:dtsg, jazoest, lsd, __comet_req:"15",
                fb_api_caller_class:"RelayModern",
                fb_api_req_friendly_name:"BIActorBasicSpokeQuery",
                server_timestamps:"true",
                variables:JSON.stringify({entity_id: entityId, action: null}),
                doc_id:"24025468177059596"
            });
            const r = await fetch("/api/graphql/", {
                method:"POST",
                headers:{"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                         "x-fb-friendly-name":"BIActorBasicSpokeQuery"},
                body:body.toString()
            });
            const txt = await r.text();
            let p={}; try{p=JSON.parse(txt);}catch(_){}
            return {status:r.status, parsed:p, body:txt.slice(0,400)};
        } catch(e) { return {err:e.toString()}; }
    }
    """
    try:
        result = page.evaluate(js, business_id)
        inner = (((result or {}).get("parsed") or {}).get("data") or {}).get("data") or {}
        if "isRestricted" in inner:
            is_restricted = bool(inner.get("isRestricted"))
            log(f"[WABA_LINK] business_id={business_id} isRestricted={is_restricted} restrictionType={inner.get('restrictionType')}")
            return is_restricted
        log(f"[WABA_LINK] unexpected restriction response shape for {business_id}: {str(result)[:300]}")
        return None
    except Exception as exc:
        log(f"[WABA_LINK] restriction probe failed for {business_id}: {exc}")
        return None


def share_waba_graphql(page, business_id: str, partner_business_id: str, waba_id: str, log=print) -> bool:
    """Share a WABA to a partner BM using useBulkAssignAssetsToPartnersMutation.

    doc_id: 9952592081492346. Grants full WhatsApp permission set (WABA_FULL_PERMISSION_TASKS).
    Navigates to the Partners page first so the SPA context and fb_dtsg/lsd tokens
    are minted in the correct page scope.

    Pre-checks BM restriction via check_bm_restricted_graphql before firing the
    mutation — FB returns HTTP 200 + result_type:"FAILURE" (no visible error) when
    the source BM is restricted, so the only reliable signal is network-side.
    Raises BmRestrictedException if the BM is restricted — caller marks as restrita.
    """
    partners_url = f"https://business.facebook.com/latest/settings/partners?business_id={business_id}"
    try:
        page.goto(partners_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WABA_LINK] navigate to Partners page failed: {exc}")

    if check_bm_restricted_graphql(page, business_id, log=log) is True:
        raise BmRestrictedException(
            f"Business Manager {business_id} está restrito — compartilhamento com parceiro será bloqueado"
        )

    variables = json.dumps({
        "businessID": business_id,
        "surfaceParams": {
            "flow_source": "BIZ_WEB",
            "entry_point": "BIZWEB_SETTINGS_PARTNERS_TAB",
            "tab": "PARTNERS",
        },
        "toBusinessID": partner_business_id,
        "assetAssignments": [{
            "asset_ids": [waba_id],
            "asset_type": "WHATSAPP_BUSINESS_ACCOUNT",
            "permitted_task_ids": WABA_FULL_PERMISSION_TASKS,
        }],
    }, separators=(",", ":"))
    js = """
    async (vars) => {
        try {
            let dtsg = "", lsd = "", uid = "";
            try { dtsg = require("DTSGInitialData").token; } catch(_) {}
            try { lsd  = require("LSD").token;             } catch(_) {}
            try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}
            if (!dtsg) { const i = document.querySelector('input[name="fb_dtsg"]'); if(i) dtsg=i.value; }
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
                fb_api_req_friendly_name:"useBulkAssignAssetsToPartnersMutation",
                server_timestamps:"true", variables:vars, doc_id:"9952592081492346"
            });
            const r = await fetch("/api/graphql/", {
                method:"POST",
                headers:{"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                         "x-fb-friendly-name":"useBulkAssignAssetsToPartnersMutation"},
                body:body.toString()
            });
            const txt = await r.text();
            let p={}; try{p=JSON.parse(txt);}catch(_){}
            const conn = p.data && p.data.business_settings_add_partner_to_assets_connection;
            const ok = !p.errors && !!conn &&
                !!(conn.results && conn.results.some(function(r){
                    return r.results && r.results.some(function(rr){ return rr.result_type === "SUCCESS"; });
                }));
            return {ok, status:r.status, body:txt.slice(0,800)};
        } catch(e) { return {ok:false, err:e.toString()}; }
    }
    """
    try:
        result = page.evaluate(js, variables)
        if result.get("ok"):
            log(f"[WABA_LINK] share mutation succeeded waba_id={waba_id} -> partner={partner_business_id}")
            return True
        log(f"[WABA_LINK] share mutation did not succeed: {result}")
        return False
    except Exception as exc:
        log(f"[WABA_LINK] share_waba_graphql exception: {exc}")
        return False


def detect_waba_partner(page, business_id: str, waba_id: str, bsp_names: list[str] | None = None, log=print) -> list[dict]:
    """Live-scan the BM's Partners tab for non-BSP partner(s) already holding
    *waba_id*. Returns [{"business_id": str, "name": str}, ...] (possibly more
    than one — the same WABA can be shared with several partners at once).

    Pure DOM/URL based, no GraphQL doc_id needed — the Partners grid renders
    each row's accessible name as the literal business name (a proper noun,
    locale-independent unlike the surrounding UI chrome). Selecting a row
    exposes its numeric id via `selected_partner_id=` in the URL and its
    assigned WhatsApp asset(s) via `a[href*="selected_asset_type=whatsapp-
    business-account"]` links carrying `selected_asset_id=` — both URL slugs,
    also locale-independent. Confirmed live against a BM with two partners
    (a real partner + the BSP "Callbell") both holding the same WABA.
    """
    partners_url = f"https://business.facebook.com/latest/settings/partners?business_id={business_id}"
    try:
        page.goto(partners_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"[WABA_LINK] detect_waba_partner navigate failed: {exc}")
        return []

    bsp_lower = [b.strip().lower() for b in (bsp_names or []) if b and b.strip()]

    try:
        names = page.evaluate(
            """() => Array.from(document.querySelectorAll('[role="row"][data-index]'))
                .map(r => { const c = r.querySelector('[role="gridcell"]'); return c ? c.innerText.trim() : ""; })
                .filter(Boolean)"""
        ) or []
    except Exception as exc:
        log(f"[WABA_LINK] detect_waba_partner row scan failed for bm={business_id}: {exc}")
        return []

    matches: list[dict] = []
    for idx, name in enumerate(names):
        if any(bsp in name.lower() for bsp in bsp_lower):
            continue
        try:
            clicked = page.evaluate(
                """(i) => {
                    const rows = Array.from(document.querySelectorAll('[role="row"][data-index]'));
                    const row = rows[i];
                    const cell = row && row.querySelector('[role="gridcell"]');
                    if (cell) { cell.click(); return true; }
                    return false;
                }""",
                idx,
            )
            if not clicked:
                continue
            page.wait_for_timeout(1200)
            m = re.search(r"selected_partner_id=(\d+)", page.url)
            if not m:
                continue
            partner_business_id = m.group(1)
            hrefs = page.eval_on_selector_all(
                'a[href*="selected_asset_type=whatsapp-business-account"]',
                "els => els.map(e => e.getAttribute('href'))",
            ) or []
            for href in hrefs:
                mm = re.search(r"selected_asset_id=(\d+)", href or "")
                if mm and mm.group(1) == str(waba_id):
                    log(f"[WABA_LINK] waba_id={waba_id} already shared with partner {name!r} ({partner_business_id})")
                    matches.append({"business_id": partner_business_id, "name": name})
                    break
        except Exception as exc:
            log(f"[WABA_LINK] detect_waba_partner row {idx} ({name!r}) failed: {exc}")
            continue

    return matches


def resolve_owning_business_id(page, log=print) -> str | None:
    """Best-effort: find a business_id this profile has access to.

    Reads it from the BM home URL redirect first, then falls back to the
    /select picker. Used when no business_id is known ahead of time (e.g.
    remark had none). Returns the first BM found.
    """
    for url in ("https://business.facebook.com/latest/settings/whatsapp_account",
                "https://business.facebook.com/select"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)
            m = re.search(r"business_id[=/](\d+)", page.url)
            if m:
                log(f"[WABA_LINK] resolved business_id={m.group(1)} from URL {page.url}")
                return m.group(1)
            hrefs = page.locator("a[href*='business_id=']").evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            ) or []
            for href in hrefs:
                mm = re.search(r"business_id=(\d+)", href or "")
                if mm:
                    log(f"[WABA_LINK] resolved business_id={mm.group(1)} from /select anchor")
                    return mm.group(1)
        except Exception as exc:
            log(f"[WABA_LINK] resolve_owning_business_id error on {url}: {exc}")
    return None
