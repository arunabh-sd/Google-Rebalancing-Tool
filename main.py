import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sheets_service import get_accounts
from google_ads_service import get_campaigns

app = FastAPI(title="ShopDeck Budget Rebalancer")

_cache = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5 minutes

@app.get("/api/accounts")
async def accounts(refresh: bool = False):
    now = time.time()
    if not refresh and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        data = get_accounts()
        _cache["data"] = data
        _cache["ts"] = now
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/campaigns/{customer_id}")
async def campaigns(customer_id: str):
    try:
        return get_campaigns(customer_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug")
async def debug():
    """Returns header → column index map and one sample row. Remove before production."""
    from sheets_service import get_raw
    try:
        return get_raw()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
