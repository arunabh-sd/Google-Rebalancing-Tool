import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ENABLED campaigns only; 7-day aggregate for stable ROAS signal
QUERY = """
    SELECT
      campaign.id,
      campaign.name,
      campaign.advertising_channel_type,
      campaign.status,
      campaign_budget.amount_micros,
      campaign_budget.explicitly_shared,
      metrics.cost_micros,
      metrics.conversions_value,
      metrics.impressions,
      metrics.search_impression_share,
      metrics.search_budget_lost_impression_share,
      metrics.search_rank_lost_impression_share
    FROM campaign
    WHERE segments.date DURING LAST_7_DAYS
      AND campaign.status = 'ENABLED'
    ORDER BY metrics.cost_micros DESC
"""

# Lightweight 3-day cost query for S/B (budget changes too fast for 7d to be reliable)
QUERY_3D = """
    SELECT
      campaign.id,
      metrics.cost_micros
    FROM campaign
    WHERE segments.date DURING LAST_3_DAYS
      AND campaign.status = 'ENABLED'
"""

CHANNEL_MAP = {
    "SEARCH":          "SEARCH",
    "SHOPPING":        "SHOPPING",
    "PERFORMANCE_MAX": "PMAX",
    "DISPLAY":         "DISPLAY",
    "MULTI_CHANNEL":   "PMAX",
    "VIDEO":           "VIDEO",
}


def _client():
    config = {
        "developer_token":   os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id":         os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret":     os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token":     os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "use_proto_plus":    True,
    }
    return GoogleAdsClient.load_from_dict(config)


def _safe_float(v) -> float:
    """Return float, treating special IS sentinel values and None as 0."""
    try:
        f = float(v)
        return f if 0.0 <= f <= 1.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def get_campaigns(customer_id: str) -> list[dict]:
    cid = customer_id.replace("-", "").strip()
    if not cid:
        return []

    client = _client()
    ga_svc = client.get_service("GoogleAdsService")

    try:
        # 3-day cost for S/B (more responsive to recent budget changes)
        cost3d_by_id: dict[str, float] = {}
        try:
            resp3d = ga_svc.search(customer_id=cid, query=QUERY_3D)
            for row in resp3d:
                cost3d_by_id[str(row.campaign.id)] = row.metrics.cost_micros / 1_000_000
        except Exception:
            pass  # fall back to 7d S/B if 3d query fails

        response = ga_svc.search(customer_id=cid, query=QUERY)
        out = []
        for row in response:
            cost7d  = row.metrics.cost_micros / 1_000_000
            value7d = row.metrics.conversions_value
            budget  = row.campaign_budget.amount_micros / 1_000_000
            cid_str = str(row.campaign.id)

            # S/GMV = cost / conv_value  (lower = more profitable)
            sgmv = round(cost7d / value7d, 6) if value7d > 0 else None
            roas = round(value7d / cost7d, 3) if cost7d > 0 else 0.0

            # S/B: use last 3 days if available (more current), else fall back to 7d
            cost3d = cost3d_by_id.get(cid_str)
            if cost3d is not None and budget > 0:
                sb = round(cost3d / (budget * 3), 4)
            elif budget > 0:
                sb = round(cost7d / (budget * 7), 4)
            else:
                sb = 0.0

            out.append({
                "id":          cid_str,
                "name":        row.campaign.name,
                "type":        CHANNEL_MAP.get(row.campaign.advertising_channel_type.name, "SEARCH"),
                "budget":      round(budget, 2),
                "cost7d":      round(cost7d, 2),
                "value7d":     round(value7d, 2),
                "impressions": int(row.metrics.impressions),
                "sgmv":        sgmv,
                "roas":        roas,
                "sb":          sb,
                "is_share":    _safe_float(row.metrics.search_impression_share),
                "is_budget":   _safe_float(row.metrics.search_budget_lost_impression_share),
                "is_rank":     _safe_float(row.metrics.search_rank_lost_impression_share),
                "shared_budget": bool(row.campaign_budget.explicitly_shared),
            })
        return out

    except GoogleAdsException as ex:
        raise RuntimeError(
            f"Google Ads API error [{cid}]: "
            + "; ".join(str(e.message) for e in ex.failure.errors)
        )
