"""
Rebalancing playbook — backend recommendation engine.

Philosophy:
  Goal is max profitable spend, not max ROAS.
  Profitable = S/GMV < db5 (5% DB breakeven).
  Guardrail 1: total spend stays ≥ current for under/on-target accounts; comes
               down toward target for overspending accounts.
  Guardrail 2: scale-ups are capped to avoid ROAS deterioration on any single campaign.
  Each account is treated as a fresh case with these as broad guidelines.
"""

from __future__ import annotations


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt(n: float) -> str:
    return "₹" + f"{int(round(n)):,}"


def _pct(f: float | None) -> str:
    if f is None:
        return "—"
    return f"{round(f * 100)}%"


# ── core logic ────────────────────────────────────────────────────────────────

def rebalance(account: dict, campaigns: list[dict]) -> dict | None:
    """
    account  : dict from sheets_service.get_accounts()
               must have db5, db0, spv, target, ySpend
    campaigns: list from google_ads_service.get_campaigns() (already ENABLED only)
    Returns  : rec dict ready for buildCard() in frontend, or None if nothing to show.
    """
    db5 = account.get("db5")   # S/GMV threshold for 5% profit
    db0 = account.get("db0")   # S/GMV threshold for breakeven (0%)
    spv = account.get("spv", "on")

    if not campaigns:
        return None

    # Drop campaigns with zero impressions — they didn't run in the window
    active = [c for c in campaigns if c["impressions"] > 0 or c["cost7d"] > 0]
    if not active:
        return None

    total_spend_7d = sum(c["cost7d"] for c in active)

    # ── per-campaign classification ───────────────────────────────────────────
    classified = []
    for c in active:
        sgmv     = c["sgmv"]          # None if no conversions
        sb       = c["sb"]            # spend/budget ratio (7d)
        is_b     = c["is_budget"]     # IS lost to budget (0–1)
        is_r     = c["is_rank"]       # IS lost to rank (0–1)
        budget   = c["budget"]
        cost7d   = c["cost7d"]

        # --- classify profitability ---
        if cost7d < 50:
            prof = "inactive"
        elif sgmv is None:
            prof = "no_conv"           # spending but zero conversions → loss signal
        elif db5 and db0:
            if sgmv < db5:
                prof = "profit"
            elif sgmv < db0:
                prof = "border"
            else:
                prof = "loss"
        else:
            prof = "unknown"           # no breakevens available

        # --- decide action ---
        rec_budget = budget
        bucket     = "leave"

        if prof == "profit":
            # Only scale if budget is actually a constraint
            budget_constrained = sb > 0.80 or is_b > 0.12
            if budget_constrained:
                # Scale proportional to: profit margin headroom + IS opportunity
                margin_headroom = max(0.0, (db5 - sgmv) / db5)   # 0→1, higher = more room
                is_opportunity  = min(is_b * 1.5, 0.35)           # IS budget lost, capped
                sb_pressure     = max(0.0, (sb - 0.75) / 0.25)   # how constrained by S/B
                # Scale factor: 1.10 minimum, 1.60 maximum
                scale = 1.0 + min(0.60, 0.10 + margin_headroom * 0.30 + is_opportunity + sb_pressure * 0.10)
                rec_budget = round(budget * scale, 0)
                bucket = "scale"
            # else: profitable but not constrained — leave

        elif prof == "border":
            # In the 0–5% zone. If heavily constrained, a small cut preserves profit margin.
            # If not constrained, watch and do nothing yet.
            if sb > 0.88 and db5 and db0:
                # Slight cut to pull it below db5 territory isn't right —
                # instead hold budget and monitor (watch, no change)
                bucket = "watch"
            else:
                bucket = "watch"

        elif prof in ("loss", "no_conv"):
            if db5 and db0:
                severity = 0.0
                if sgmv and db0:
                    severity = min((sgmv - db0) / db0, 1.0)   # 0→1: how far past db0
                cut_factor = 0.70 - severity * 0.20            # 0.50–0.70
                rec_budget = round(budget * cut_factor, 0)
                bucket = "cut"
            elif prof == "no_conv":
                # No breakevens but spending with zero conversions — cut to 50%
                rec_budget = round(budget * 0.50, 0)
                bucket = "cut"

        # Minimum rec_budget floor (don't cut below ₹100/day)
        rec_budget = max(rec_budget, 100.0)

        classified.append({**c, "prof": prof, "bucket": bucket, "rec_budget": rec_budget})

    # ── spend guardrail ───────────────────────────────────────────────────────
    # Estimate new 7d spend for each campaign after budget change.
    # When budget increases, S/B tends to drop slightly (more inventory available).
    # When budget decreases, actual spend falls roughly proportionally to cut.
    def est_new_spend(r: dict) -> float:
        b = r["rec_budget"]
        sb = r["sb"]
        if r["bucket"] == "leave" or r["bucket"] == "watch":
            return r["cost7d"]
        elif r["bucket"] == "cut":
            return b * min(sb, 0.90) * 7
        else:  # scale
            return b * min(sb * 0.88, 0.95) * 7   # S/B drops ~12% on scale-up

    proj_spend_7d = sum(est_new_spend(r) for r in classified)

    if spv == "over":
        # Pull spend down toward current — allow cuts to do their job, restrain scales
        if proj_spend_7d > total_spend_7d * 1.03:
            adj = total_spend_7d / proj_spend_7d
            for r in classified:
                if r["bucket"] == "scale":
                    r["rec_budget"] = max(r["budget"], round(r["rec_budget"] * adj, 0))
    else:
        # Under / on-target: don't let total spend drop below current
        if proj_spend_7d < total_spend_7d * 0.92:
            for r in classified:
                if r["bucket"] == "cut":
                    r["rec_budget"] = max(r["rec_budget"], round(r["budget"] * 0.80, 0))

    # ── filter trivial changes (< 8% of budget) and format ───────────────────
    result_campaigns = []
    for r in classified:
        delta  = r["rec_budget"] - r["budget"]
        pct_ch = abs(delta) / r["budget"] if r["budget"] > 0 else 0

        if pct_ch < 0.08:
            r["bucket"]     = "leave" if r["bucket"] != "watch" else "watch"
            r["rec_budget"] = r["budget"]
            delta           = 0.0

        dir_    = "p" if delta > 0 else ("n" if delta < 0 else "f")
        delta_s = ("+" if delta > 0 else "") + _fmt(delta) if delta != 0 else "₹0"

        result_campaigns.append({
            "id":     r["id"],
            "name":   r["name"],
            "type":   r["type"],
            "roas":   r["roas"],
            "sgmv":   r["sgmv"],
            "sb":     r["sb"],
            "cost7d": r["cost7d"],
            "budg":   _fmt(r["budget"]),
            "rec":    _fmt(r["rec_budget"]),
            "dir":    dir_,
            "delta":  delta_s,
            "sbPct":  _pct(r["sb"]),
            "bucket": r["bucket"],
            "prof":   r["prof"],
        })

    # ── signals ───────────────────────────────────────────────────────────────
    signals = []
    n_scale   = sum(1 for c in result_campaigns if c["bucket"] == "scale")
    n_cut     = sum(1 for c in result_campaigns if c["bucket"] == "cut")
    n_watch   = sum(1 for c in result_campaigns if c["bucket"] == "watch")
    n_no_conv = sum(1 for c in result_campaigns if c["prof"] == "no_conv")

    if n_scale:
        signals.append({"c": "green", "t": f"{n_scale} campaign{'s' if n_scale>1 else ''} profitable & budget-constrained → scale"})
    if n_cut:
        signals.append({"c": "red",   "t": f"{n_cut} campaign{'s' if n_cut>1 else ''} below 0% DB breakeven → cut"})
    if n_watch:
        signals.append({"c": "amber", "t": f"{n_watch} campaign{'s' if n_watch>1 else ''} in 0–5% zone → watch"})
    if n_no_conv:
        signals.append({"c": "red",   "t": f"{n_no_conv} campaign{'s' if n_no_conv>1 else ''} spending with zero conversions"})

    # Return rec only if there's at least one actionable change
    has_action = n_scale + n_cut > 0
    if not has_action and not signals:
        return None

    return {
        "campaigns":     result_campaigns,
        "signals":       signals,
        "projRoas":      "—",
        "flag":          "",
        "recentChanges": None,
    }
