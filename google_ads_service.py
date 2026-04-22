import os
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

QUERY = """
    SELECT
      campaign.id,
      campaign.name,
      campaign.advertising_channel_type,
      campaign.status,
      campaign_budget.amount_micros,
      metrics.conversions_value,
      metrics.cost_micros
    FROM campaign
    WHERE segments.date DURING LAST_7_DAYS
      AND campaign.status != 'REMOVED'
    ORDER BY metrics.cost_micros DESC
"""

CHANNEL_MAP = {
    "SEARCH": "SEARCH",
    "SHOPPING": "SHOPPING",
    "PERFORMANCE_MAX": "PMAX",
    "DISPLAY": "DISPLAY",
    "MULTI_CHANNEL": "PMAX",
    "VIDEO": "VIDEO",
}


def _client():
    config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id":       os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret":   os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token":   os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "use_proto_plus":  True,
    }
    return GoogleAdsClient.load_from_dict(config)


def get_campaigns(customer_id: str) -> list[dict]:
    cid = customer_id.replace("-", "").strip()
    if not cid:
        return []

    client = _client()
    ga_service = client.get_service("GoogleAdsService")

    try:
        response = ga_service.search(customer_id=cid, query=QUERY)
        out = []
        for row in response:
            cost  = row.metrics.cost_micros / 1_000_000
            value = row.metrics.conversions_value
            roas  = round(value / cost, 2) if cost > 0 else 0.0
            budget = round(row.campaign_budget.amount_micros / 1_000_000, 0)
            ctype  = CHANNEL_MAP.get(
                row.campaign.advertising_channel_type.name, "SEARCH"
            )
            out.append({
                "id":      str(row.campaign.id),
                "name":    row.campaign.name,
                "type":    ctype,
                "budget":  budget,
                "roas":    roas,
                "cost7d":  round(cost, 0),
                "value7d": round(value, 0),
            })
        return out
    except GoogleAdsException as ex:
        raise RuntimeError(
            f"Google Ads API error for {cid}: "
            + "; ".join(str(e.message) for e in ex.failure.errors)
        )
