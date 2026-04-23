import os
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sheets_service import get_accounts, get_raw
from google_ads_service import get_campaigns
from playbook import rebalance as rule_rebalance

app = FastAPI(title="ShopDeck Budget Rebalancer")

_cache = {"data": None, "ts": 0}
CACHE_TTL  = 300
_executor  = ThreadPoolExecutor(max_workers=20)


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
    ids: list[str]
    mode: str = "neutral"


@app.post("/api/rebalance")
async def rebalance_accounts(req: RebalanceRequest):
    loop = asyncio.get_event_loop()

    try:
        all_accounts = _get_accounts_cached()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sheet error: {e}")

    id_map = {a["id"]: a for a in all_accounts}

    # ── fetch campaigns for all selected accounts in parallel ─────────────────
    async def fetch_one(sid: str):
        acct = id_map.get(sid)
        if not acct or not acct.get("adId"):
            return sid, acct, []
        try:
            camps = await loop.run_in_executor(_executor, get_campaigns, acct["adId"])
            return sid, acct, camps
        except Exception as e:
            print(f"Campaign fetch error [{sid}]: {e}")
            return sid, acct, []

    fetch_results = await asyncio.gather(*[fetch_one(sid) for sid in req.ids])

    pairs = [(acct, camps) for _, acct, camps in fetch_results
             if acct and camps]

    # ── LLM rebalance (all accounts in one/few batched calls) ────────────────
    result = {}
    use_llm = os.environ.get("USE_LLM", "true").lower() == "true"

    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from llm_playbook import rebalance_all
            result = await loop.run_in_executor(_executor, rebalance_all, pairs, req.mode)
        except Exception as e:
            print(f"LLM rebalance error: {e}")

    # ── rule-based fallback for accounts LLM missed ───────────────────────────
    processed = set(result.keys())
    for sid, acct, camps in fetch_results:
        if sid in processed or not acct or not camps:
            continue
        try:
            rec = rule_rebalance(acct, camps, req.mode)
            if rec:
                result[sid] = rec
        except Exception as e:
            result[sid] = {
                "campaigns": [], "signals": [{"c": "red", "t": str(e)[:120]}],
                "projRoas": "—", "flag": "", "recentChanges": None,
            }

    return result


@app.get("/api/campaigns/{customer_id}")
async def campaigns_debug(customer_id: str):
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
