// GraphQL doc_id map — extracted from I:\Verificador Interface\check_restriction.har and mirrored
// from agent/services/facebook_link.py (same repo, same doc_ids). Runs in the MAIN world so bridge.js
// can build the request bodies; kept in its own file (rather than inline in bridge.js) to make a
// future doc_id rotation a one-file edit, same convention as waba_inspector/lib/queries.js.
(function () {
  const NS = (window.__ML_QUERIES__ = window.__ML_QUERIES__ || {});

  // Full WhatsApp permission set granted to a partner BM on share — verbatim copy of
  // agent/services/facebook_link.py:WABA_FULL_PERMISSION_TASKS.
  NS.WABA_FULL_PERMISSION_TASKS = [
    "1178671679425452", "2267064093541969", "468633770384254", "337342957148859",
    "1455369811941901", "3507194632893280", "934369393579850", "1292349377982198",
    "281909864313192", "446593557821945", "3863350093762893", "314336691716181",
    "507689495019576", "617932692057625",
  ];

  NS.queries = {
    restriction: {
      name: "BIActorBasicSpokeQuery",
      docId: "24025468177059596",
      vars: (ctx) => ({ entity_id: ctx.businessId, action: null }),
    },
    // Same enum vocabulary (TIER_50 … TIER_UNLIMITED) the dashboard already renders via
    // snapshot.messaging_limit_tier (app/services/sync_service.py, populated from the official
    // whatsapp_business_manager_messaging_limit Graph API field) — this internal query is the source
    // WhatsApp Manager's own UI reads from, ported from
    // I:\Verificador Interface\waba_inspector\lib\queries.js:14-18. Wired up so the popup can show it
    // and register it fresher than the next background sync.
    limits: {
      name: "BizWebCometWhatsAppManagerBusinessMessagingLimitsRootQuery",
      docId: "27092965037001831",
      vars: (ctx) => ({ businessID: ctx.businessId, wabaID: ctx.wabaId }),
    },
    listWabas: {
      name: "BizKitSettingsBusinessAssetsListContainerQuery",
      docId: "27483473261255388",
      vars: (ctx) => ({
        businessID: ctx.businessId,
        assetTypes: ["WHATSAPP_BUSINESS_ACCOUNT"],
        searchTerm: null,
        orderBy: null,
        assetFilters: {
          whatsapp_business_account_statuses: ["ACTIVE", "ONBOARDING"],
          exclude_catalog_segments: null,
        },
        globalFilters: {},
        shouldSkip: false,
        shouldCountAdmin: false,
        count: 10,
      }),
    },
    // No separate "validate partner id" / "list existing partners" pre-checks — mirrors
    // agent/services/facebook_link.py:share_waba_graphql, which fires this mutation directly after the
    // restriction pre-check and trusts result_type SUCCESS/FAILURE as the only ground truth. Those two
    // extra GraphQL calls exist in the Meta UI flow but their response shapes were never captured with a
    // body in check_restriction.har, so guessing at them would add risk without a confirmed payoff.
    sharePartner: {
      name: "useBulkAssignAssetsToPartnersMutation",
      docId: "9952592081492346",
      vars: (ctx) => ({
        businessID: ctx.businessId,
        surfaceParams: {
          flow_source: "BIZ_WEB",
          entry_point: "BIZWEB_SETTINGS_PARTNERS_TAB",
          tab: "PARTNERS",
        },
        toBusinessID: ctx.partnerBusinessId,
        assetAssignments: [{
          asset_ids: [ctx.wabaId],
          asset_type: "WHATSAPP_BUSINESS_ACCOUNT",
          permitted_task_ids: NS.WABA_FULL_PERMISSION_TASKS,
        }],
      }),
    },
  };
})();
