import os
import json
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID  = "1qNCgXO3xXfNMR11xEduNTZcydNg7klxgnDZkWUzfxag"
SHEET_TAB = "performance tracker"
SCOPES    = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# 0-based column indices — map to "performance tracker" header row
C = {
    "sid":        0,   # Seller Id
    "name":       1,   # Seller Name
    "ad_id":      2,   # Ad Account ID
    # 3: Temp POC (ignored)
    "ggl":        4,   # POC Assigned = Google GL
    "gl":         5,   # GL
    "gm":         6,   # GM
    "tag":        7,   # >5000 / <5000
    "y_sgmv":     8,   # Yesterday S/GMV
    # 9: Day before yest (unused in UI for now)
    # 10: Delta (ignore)
    "l2d_sgmv":   11,
    "l3d_sgmv":   12,
    "l6d_sgmv":   13,
    "wtd_sgmv":   14,  # WTD S/GMV ← primary profitability signal
    "lw_sgmv":    15,  # Last week S/GMV
    "lw_db_sgmv": 16,  # Last week DB S/GMV (dashboard-reported)
    "be5":        17,  # 5% BE (unadjusted — fallback only)
    "be0":        18,  # 0% BE (unadjusted — fallback only)
    "mult":       19,  # Multiplier = LW DB S/GMV ÷ LW Actual S/GMV
    "db10":       20,  # 10% DB
    "db5":        21,  # 5% DB (dashboard-adjusted) ← primary breakeven
    "db0":        22,  # 0% DB ← hard floor
    "lw_profit":  23,  # Last week Profit % of NMV
    "pred_profit":24,  # This week Predicted Profit
    "arr_pct":    25,  # ARR % contribution
    "lw_arr_bh":  26,  # LW ARR BH%
    "w2_profit":  27,  # Week -2 Profit
    "y_pl_db":    28,  # Y PLDb (dashboard PnL bucket)
    "y_pnl":      29,  # Y pnL  (1=profit>5%, 2=0-5%, 3=loss, 4=not running)
    "l2d_pnl":    30,
    "l3d_pnl":    31,
    "l6d_pnl":    32,
    "wtd_pnl":    33,
    "lw_pnl":     34,
    "llw_pnl":    35,
    # 36-40: ignored (reason codes, comments)
    "week_tgt":   41,  # Week Target (₹ daily spend target)
    "y_spend":    42,  # Yesterday Spend (₹)
    # 43: Day Before Yesterday spend
    # 44: Delta
    "tgt_delta":  45,  # Week Target Delta
    "l3d_sp":     46,  # Last 3d Spends (daily avg)
    "wtd_sp":     47,  # WTD Spends (daily avg)
    "lw_sp":      48,  # Last week Spends (daily avg)
    "llw_sp":     49,  # Last to Last week Spend (daily avg)
    "sat_spend":  50,  # Last Saturday Spend
    # 51-62: ignored
    "y_gmv":      63,  # Yesterday GMV
}

MISSING = {"NA", "#VALUE!", "#N/A", "#REF!", "#DIV/0!", "NANA", "-", ""}


def _pct(v) -> float | None:
    """Parse '16.88%' → 0.1688. Returns None for missing/error values."""
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
    """Parse '12,223' → 12223.0. Returns None for missing/error values."""
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
    """Parse PnL bucket (1/2/3/4). Returns default for missing data."""
    try:
        n = int(float(str(v).strip()))
        return n if 1 <= n <= 4 else default
    except (ValueError, TypeError):
        return default


def _get(row: list, idx: int, default: str = "") -> str:
    try:
        return row[idx] if idx < len(row) else default
    except IndexError:
        return default


def _prof(wtd, db5, db0) -> str:
    """Derive profitability status from S/GMV vs breakeven thresholds."""
    if wtd is None:
        return "be"
    if db5 and wtd <= db5:
        return "profit"
    if db0 and wtd <= db0:
        return "be"
    return "loss"


def _spv(spend, target) -> str:
    """Derive spend-vs-target status."""
    if not spend or not target:
        return "on"
    r = spend / target
    if r < 0.90:
        return "under"
    if r > 1.05:
        return "over"
    return "on"


def _client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_accounts() -> list[dict]:
    gc = _client()
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    rows = ws.get_all_values()

    if not rows:
        return []

    out = []
    for row in rows[1:]:  # skip header row
        ad_id = _get(row, C["ad_id"]).strip()
        if not ad_id:
            continue  # skip blank rows

        # Breakeven: prefer dashboard-adjusted (DB) columns, fall back to BE
        db5 = _pct(_get(row, C["db5"])) or _pct(_get(row, C["be5"]))
        db0 = _pct(_get(row, C["db0"])) or _pct(_get(row, C["be0"]))

        wtd    = _pct(_get(row, C["wtd_sgmv"]))
        y_sp   = _num(_get(row, C["y_spend"]))
        tgt    = _num(_get(row, C["week_tgt"]))
        mult   = _pct(_get(row, C["mult"]))   # stored as % e.g. '96.39%' → 0.9639

        # PnL trend array: [LLW, LW, 6d, 3d, 2d, Y, WTD]
        pnl = [
            _bkt(_get(row, C["llw_pnl"])),
            _bkt(_get(row, C["lw_pnl"])),
            _bkt(_get(row, C["l6d_pnl"])),
            _bkt(_get(row, C["l3d_pnl"])),
            _bkt(_get(row, C["l2d_pnl"])),
            _bkt(_get(row, C["y_pnl"])),
            _bkt(_get(row, C["wtd_pnl"])),
        ]

        out.append({
            "id":          _get(row, C["sid"]).strip(),
            "name":        _get(row, C["name"]).strip(),
            "adId":        ad_id,
            "ggl":         _get(row, C["ggl"]).strip(),
            "gl":          _get(row, C["gl"]).strip(),
            "gm":          _get(row, C["gm"]).strip(),
            "tag":         _get(row, C["tag"]).strip(),
            "wtdSGmv":     wtd,
            "db5":         db5,
            "db0":         db0,
            "be5":         _pct(_get(row, C["be5"])),
            "lw":          _pct(_get(row, C["lw_sgmv"])),
            "ySpend":      y_sp,
            "target":      tgt,
            "pnl":         pnl,
            "predProfit":  _pct(_get(row, C["pred_profit"])),
            "arrPct":      _pct(_get(row, C["arr_pct"])),
            "mult":        mult,
            "prof":        _prof(wtd, db5, db0),
            "spv":         _spv(y_sp, tgt),
        })

    return out
