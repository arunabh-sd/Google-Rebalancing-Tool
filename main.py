import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sheets_service import get_accounts, get_raw
from google_ads_service import get_campaigns
from playbook import rebalance

app = FastAPI(title="ShopDeck Budget Rebalancer")

_cache = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5 minutes


def _get_accounts_cached(refresh: bool = False) -> list[dict]:
    now = time.time()
    if not refresh and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    data = get_accounts()
    _cache["data"] = data
    _cache["ts"]   = now
    return data


@app.get("/api/accounts")
async def accounts(refresh: bool = False):
    try:
        return _get_accounts_cached(refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RebalanceRequest(BaseModel):
    ids: list[str]   # seller IDs from the sheet


@app.post("/api/rebalance")
async def rebalance_accounts(req: RebalanceRequest):
    try:
        all_accounts = _get_accounts_cached()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sheet error: {e}")

    id_map = {a["id"]: a for a in all_accounts}
    result = {}

    for sid in req.ids:
        acct = id_map.get(sid)
        if not acct or not acct.get("adId"):
            continue
        try:
            camps = get_campaigns(acct["adId"])
            rec   = rebalance(acct, camps)
            if rec:
                result[sid] = rec
        except Exception as e:
            result[sid] = {"error": str(e), "campaigns": [], "signals": [{"c": "red", "t": str(e)[:120]}]}

    return result


@app.get("/api/campaigns/{customer_id}")
async def campaigns_debug(customer_id: str):
    """Debug: raw campaign data for one account."""
    try:
        return get_campaigns(customer_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/debug")
async def debug():
    try:
        return get_raw()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
