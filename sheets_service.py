import os
import json
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID  = "1qNCgXO3xXfNMR11xEduNTZcydNg7klxgnDZkWUzfxag"
SHEET_TAB = "Performance Tracker"
SCOPES    = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

MISSING = {"NA", "#VALUE!", "#N/A", "#REF!", "#DIV/0!", "NANA", "-", ""}

# Canonical header name → key used in code
# Each entry is (key, [list of possible header names to match])
HEADER_MAP = [
    ("sid",         ["Seller Id"]),
    ("name",        ["Seller Name"]),
    ("ad_id",       ["Ad Account ID"]),
    ("ggl",         ["POC Assigned"]),
    ("gl",          ["GL"]),
    ("gm",          ["GM"]),
    ("tag",         ["Tag"]),
    ("wtd_sgmv",    ["WTD S/Gmv", "WTD S/GMV"]),
    ("lw_sgmv",     ["Last weeK S/GMV", "Last week S/GMV"]),
    ("lw_db_sgmv",  ["Last week DB S/gmv", "Last week DB S/GMV"]),
    ("be5",         ["5% BE"]),
    ("be0",         ["0% BE"]),
    ("mult",        ["Multiplier"]),
    ("db5",         ["5% DB"]),
    ("db0",         ["0% DB"]),
    ("pred_profit", ["This week Predicted Profit"]),
    ("arr_pct",     ["ARR % (as per Yest Gmv)", "ARR %"]),
    ("y_pl_db",     ["Y PLDb"]),
    ("y_pnl",       ["Y pnL"]),
    ("l2d_pnl",     ["L2d PL"]),
    ("l3d_pnl",     ["L3d PL"]),
    ("l6d_pnl",     ["L6d PnL", "6d PnL"]),
    ("wtd_pnl",     ["WTD PnL"]),
    ("lw_pnl",      ["LW PnL"]),
    ("llw_pnl",     ["LLW PnL"]),
    ("week_tgt",    ["Week Target"]),
    ("y_spend",     ["Yesterday Spend"]),
]


def _build_col_map(headers: list[str]) -> dict[str, int]:
    """Build key → column index from the actual header row."""
    # Normalize headers once
    norm = [h.strip() for h in headers]

    col = {}
    for key, candidates in HEADER_MAP:
        found = None
        for c in candidates:
            c_lower = c.lower()
            for i, h in enumerate(norm):
                if h.lower() == c_lower:
                    found = i
                    break
            if found is not None:
                break
        if found is not None:
            col[key] = found
        # If not found, key will be absent — callers get "" via _get fallback

    return col


def _pct(v) -> float | None:
    """'16.88%' → 0.1688 | '0.2149' → 0.2149 | blank/error → None"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in MISSING:
        return None
    if s.endswith("%"):
        try:
            return round(float(s[:-1]) / 100, 6)
        except ValueError:
            return None
    try:
        return round(float(s), 6)
    except ValueError:
        return None


def _num(v) -> float | None:
    """'12,223' → 12223.0 | blank/error → None"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("₹", "").replace(" ", "")
    if not s or s in MISSING:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bkt(v, default: int = 4) -> int:
    """PnL bucket string → int (1/2/3/4)"""
    try:
        n = int(float(str(v).strip()))
        return n if 1 <= n <= 4 else default
    except (ValueError, TypeError):
        return default


def _get(row: list, col: dict, key: str, default: str = "") -> str:
    idx = col.get(key)
    if idx is None:
        return default
    try:
        return row[idx] if idx < len(row) else default
    except IndexError:
        return default


def _prof(wtd, db5, db0) -> str:
    if wtd is None:
        return "be"
    if db5 and wtd <= db5:
        return "profit"
    if db0 and wtd <= db0:
        return "be"
    return "loss"


def _spv(spend, target) -> str:
    if spend is None or target is None or target == 0:
        return "on"
    r = spend / target
    if r < 0.95:
        return "under"
    if r > 1.05:
        return "over"
    return "on"


def _client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(creds)


def get_raw() -> dict:
    """Debug: return headers with indices and first data row."""
    gc = _client()
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    rows = ws.get_all_values()
    headers = rows[0] if rows else []
    col = _build_col_map(headers)
    return {
        "col_map": col,
        "headers": {str(i): h for i, h in enumerate(headers)},
        "sample":  rows[1] if len(rows) > 1 else [],
    }


def get_accounts() -> list[dict]:
    gc = _client()
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    rows = ws.get_all_values()

    if not rows:
        return []

    col = _build_col_map(rows[0])

    out = []
    for row in rows[1:]:
        ad_id = _get(row, col, "ad_id").strip()
        if not ad_id:
            continue

        db5  = _pct(_get(row, col, "db5"))  or _pct(_get(row, col, "be5"))
        db0  = _pct(_get(row, col, "db0"))  or _pct(_get(row, col, "be0"))
        wtd  = _pct(_get(row, col, "wtd_sgmv"))
        y_sp = _num(_get(row, col, "y_spend"))
        tgt  = _num(_get(row, col, "week_tgt"))
        mult = _pct(_get(row, col, "mult"))

        pnl = [
            _bkt(_get(row, col, "llw_pnl")),
            _bkt(_get(row, col, "lw_pnl")),
            _bkt(_get(row, col, "l6d_pnl")),
            _bkt(_get(row, col, "l3d_pnl")),
            _bkt(_get(row, col, "l2d_pnl")),
            _bkt(_get(row, col, "y_pnl")),
            _bkt(_get(row, col, "wtd_pnl")),
        ]

        out.append({
            "id":         _get(row, col, "sid").strip(),
            "name":       _get(row, col, "name").strip(),
            "adId":       ad_id,
            "ggl":        _get(row, col, "ggl").strip(),
            "gl":         _get(row, col, "gl").strip(),
            "gm":         _get(row, col, "gm").strip(),
            "tag":        _get(row, col, "tag").strip(),
            "wtdSGmv":    wtd,
            "db5":        db5,
            "db0":        db0,
            "be5":        _pct(_get(row, col, "be5")),
            "lw":         _pct(_get(row, col, "lw_sgmv")),
            "ySpend":     y_sp,
            "target":     tgt,
            "pnl":        pnl,
            "predProfit": _pct(_get(row, col, "pred_profit")),
            "arrPct":     _pct(_get(row, col, "arr_pct")),
            "mult":       mult,
            "prof":       _prof(wtd, db5, db0),
            "spv":        _spv(y_sp, tgt),
        })

    return out
