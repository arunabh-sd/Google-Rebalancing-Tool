"""
Rebalancing playbook — spend-neutral budget reallocation engine.

Core principle:
  Reallocate spend from worst-ROAS campaigns to best-ROAS campaigns.
  Budget changes only matter if the campaign is budget-constrained (high S/B).

  Spend prediction model:
    Cut: released_spend_7d  = max(0, cost7d - rec_budget × 7)   [budget becomes binding]
    Scale: absorbed_spend_7d = (rec_budget - budget) × sb × 0.88 × 7  [S/B drops ~12%]

  Guardrail 1: total projected spend ≥ current for under/on-target accounts.
  Guardrail 2: max single-campaign scale capped at 50% (30% in all-loss scenario).
"""
from __future__ import annotations


def _fmt(n: float) -> str:
    return "₹" + f"{int(round(n)):,}"


def _exp_spend_delta_7d(budget: float, rec_budget: float, sb: float) -> float:
    if rec_budget > budget:
        return (rec_budget - budget) * sb * 0.88 * 7
    elif rec_budget < budget:
        daily = budget * min(sb, 1.0)
        return (min(rec_budget, daily) - daily) * 7
    return 0.0


def _pct(f: float | None) -> str:
    if f is None:
        return "—"
    return f"{round(f * 100)}%"


def _released_spend(budget: float, rec_budget: float, sb: float, cost7d: float) -> float:
    """Spend released (7d) when cutting budget from budget → rec_budget."""
    if rec_budget >= budget:
        return 0.0
    # Only releases spend if the cut goes below current daily spend
    daily_spend = min(budget * sb, budget)
    new_daily   = min(rec_budget, daily_spend)
    return max(0.0, (daily_spend - new_daily) * 7)


def _absorbed_spend(budget: float, rec_budget: float, sb: float) -> float:
    """Extra spend absorbed (7d) when scaling budget from budget → rec_budget."""
    if rec_budget <= budget:
        return 0.0
    # New budget increment × current S/B × 0.88 (S/B drops on scale-up) × 7 days
    return (rec_budget - budget) * sb * 0.88 * 7


def rebalance(account: dict, campaigns: list[dict], mode: str = "neutral") -> dict | None:
    db5 = account.get("db5")   # S/GMV threshold — below = profitable
    db0 = account.get("db0")   # S/GMV threshold — above = loss
    spv = account.get("spv", "on")
    revops_under = (mode == "revops" and spv == "under")
    revops_over  = (mode == "revops" and spv == "over")

    if not campaigns:
        return None

    active = [c for c in campaigns if c["cost7d"] > 50 or c["impressions"] > 100]
    if not active:
        return None

    total_spend_7d = sum(c["cost7d"] for c in active)

    # ── 1. classify each campaign ─────────────────────────────────────────────
    for c in active:
        sgmv = c["sgmv"]
        if c["cost7d"] < 50:
            c["cls"] = "inactive"
        elif sgmv is None:
            c["cls"] = "no_conv"
        elif db5 and db0:
            if sgmv < db5:
                c["cls"] = "profit"
            elif sgmv < db0:
                c["cls"] = "border"
            else:
                c["cls"] = "loss"
        else:
            c["cls"] = "unknown"

    # ── 2. determine scenario and assign roles ────────────────────────────────
    ranked     = sorted(active, key=lambda c: c["roas"], reverse=True)
    n          = len(ranked)
    all_loss   = db5 and db0 and all(
        c["cls"] in ("loss", "no_conv", "inactive", "unknown")
        for c in ranked
    )
    has_profit = any(c["cls"] == "profit" for c in ranked)

    for i, c in enumerate(ranked):
        cls = c["cls"]
        sb  = c["sb"]
        is_b = c.get("is_budget", 0)

        if all_loss:
            # Relative reallocation: top 35% ROAS = scale candidates, bottom 35% = cut
            top_n    = max(1, round(n * 0.35))
            bottom_n = max(1, round(n * 0.35))
            if i < top_n:
                c["role"] = "scale"
            elif i >= n - bottom_n:
                c["role"] = "cut"
            else:
                c["role"] = "leave"
        else:
            if cls == "profit":
                c["role"] = "scale"
            elif cls in ("loss", "no_conv"):
                c["role"] = "cut"
            elif cls == "border":
                c["role"] = "watch"
            else:
                c["role"] = "leave"

    # ── 3. compute raw budget recommendations ─────────────────────────────────
    max_scale = 1.30 if all_loss else 1.60   # guardrail 2

    for c in ranked:
        budget  = c["budget"]
        sb      = c["sb"]
        sgmv    = c["sgmv"]
        is_b    = c.get("is_budget", 0)
        role    = c["role"]

        if role == "scale":
            if all_loss:
                # Conservative: scale proportional to how much better this is vs account average
                avg_roas = (sum(x["roas"] for x in ranked if x["roas"] > 0) / n) or 1
                rel_edge = min((c["roas"] - avg_roas) / avg_roas, 0.5)  # 0 → 0.5
                scale    = 1.15 + rel_edge * 0.30                        # 1.15 → 1.30
            else:
                margin  = max(0.0, (db5 - sgmv) / db5) if db5 and sgmv else 0
                is_opp  = min(is_b * 1.5, 0.35)
                sb_p    = max(0.0, (sb - 0.75) / 0.25)
                scale   = 1.0 + min(max_scale - 1.0, 0.10 + margin * 0.30 + is_opp + sb_p * 0.10)
            c["rec_budget"] = round(budget * min(scale, max_scale), 0)

        elif role == "cut":
            if sgmv is None:
                # No conversions: cut to 50%
                c["rec_budget"] = round(budget * 0.50, 0)
            elif db0 and sgmv > db0:
                severity        = min((sgmv - db0) / db0, 1.0)
                cut_floor       = 0.75 if all_loss else 0.60   # gentler in all-loss (maintain spend)
                cut_factor      = cut_floor - severity * (0.15 if all_loss else 0.20)
                c["rec_budget"] = round(budget * max(cut_factor, 0.40), 0)
            else:
                c["rec_budget"] = round(budget * 0.65, 0)

        else:
            c["rec_budget"] = budget

        c["rec_budget"] = max(c["rec_budget"], 100.0)

    # ── 4. spend transfer check & guardrail enforcement ───────────────────────
    released = sum(_released_spend(c["budget"], c["rec_budget"], c["sb"], c["cost7d"])
                   for c in ranked if c["role"] == "cut")
    absorbed = sum(_absorbed_spend(c["budget"], c["rec_budget"], c["sb"])
                   for c in ranked if c["role"] == "scale")

    # RevOps/underspend: skip spend-neutral constraint — goal is to increase spend
    if not revops_under and (all_loss or (not has_profit)):
        # Scale campaigns should only absorb what cut campaigns release (spend-neutral)
        if absorbed > 0 and released < absorbed:
            adj = released / absorbed if absorbed > 0 else 0
            for c in ranked:
                if c["role"] == "scale":
                    excess = c["rec_budget"] - c["budget"]
                    c["rec_budget"] = round(c["budget"] + excess * adj, 0)
                    c["rec_budget"] = max(c["rec_budget"], 100.0)

    # Guardrail 1: for under/on-target in neutral mode, total spend must not drop
    proj_spend = total_spend_7d - released + absorbed
    if not revops_under and spv in ("under", "on") and proj_spend < total_spend_7d * 0.92:
        shortfall = total_spend_7d * 0.92 - proj_spend
        for c in ranked:
            if c["role"] == "cut" and shortfall > 0:
                restore  = min(shortfall / 7 / max(c["sb"], 0.1), c["budget"] - c["rec_budget"])
                c["rec_budget"] = round(c["rec_budget"] + restore, 0)
                shortfall -= restore * c["sb"] * 7

    # RevOps/overspend: soften all cuts to reduce spend aggressively
    if revops_over and spv == "over":
        for c in ranked:
            if c["role"] in ("watch", "leave") and c["cls"] not in ("profit",):
                c["role"]       = "cut"
                c["rec_budget"] = round(c["budget"] * 0.80, 0)
                c["rec_budget"] = max(c["rec_budget"], 100.0)

    # ── 5. filter trivial changes and format output ───────────────────────────
    result_campaigns = []
    for c in ranked:
        budget = c["budget"]
        rec    = c["rec_budget"]
        delta  = rec - budget
        pct_ch = abs(delta) / budget if budget > 0 else 0

        if pct_ch < 0.08:
            c["role"]       = "watch" if c["role"] == "watch" else "leave"
            c["rec_budget"] = budget
            delta = 0.0

        dir_    = "p" if delta > 0 else ("n" if delta < 0 else "f")
        delta_s = ("+" if delta > 0 else "") + _fmt(delta) if delta != 0 else "₹0"
        spend_d = _exp_spend_delta_7d(budget, c["rec_budget"], c["sb"])

        result_campaigns.append({
            "id":                c["id"],
            "name":              c["name"],
            "type":              c["type"],
            "roas":              c["roas"],
            "sgmv":              c["sgmv"],
            "sb":                c["sb"],
            "cost7d":            c["cost7d"],
            "budg":              _fmt(budget),
            "rec":               _fmt(c["rec_budget"]),
            "dir":               dir_,
            "delta":             delta_s,
            "sbPct":             _pct(c["sb"]),
            "bucket":            c["role"] if c["role"] in ("scale", "cut", "watch") else "leave",
            "prof":              c["cls"],
            "expSpendDeltaDay":  round(spend_d / 7, 0),
        })

    # ── 6. signals ────────────────────────────────────────────────────────────
    signals = []
    n_scale   = sum(1 for c in result_campaigns if c["bucket"] == "scale")
    n_cut     = sum(1 for c in result_campaigns if c["bucket"] == "cut")
    n_watch   = sum(1 for c in result_campaigns if c["bucket"] == "watch")
    n_no_conv = sum(1 for c in result_campaigns if c["prof"] == "no_conv")

    if all_loss:
        signals.append({"c": "amber", "t": "All campaigns below breakeven — reallocating from worst to best ROAS"})
    if n_scale:
        signals.append({"c": "green", "t": f"{n_scale} campaign{'s' if n_scale>1 else ''} scaled up"})
    if n_cut:
        signals.append({"c": "red",   "t": f"{n_cut} campaign{'s' if n_cut>1 else ''} cut"})
    if n_watch:
        signals.append({"c": "amber", "t": f"{n_watch} campaign{'s' if n_watch>1 else ''} in 0–5% zone — watching"})
    if n_no_conv:
        signals.append({"c": "red",   "t": f"{n_no_conv} campaign{'s' if n_no_conv>1 else ''} spending with zero conversions"})

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
