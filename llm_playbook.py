"""
LLM-based rebalancing engine.

All selected accounts are sent in ONE Claude API call using a compact prompt format.
For > BATCH_SIZE accounts, they are split into parallel batches.

Token budget (estimate for 100 accounts × 10 campaigns):
  Input:  ~35K tokens  → ~$0.11 @ Sonnet pricing
  Output: ~12K tokens  → ~$0.18
  Total:  ~$0.29 / run
"""
from __future__ import annotations

import os
import math
import anthropic

BATCH_SIZE   = 30          # accounts per API call
MODEL        = "claude-sonnet-4-6"
MAX_TOKENS   = 8192

# ── spend delta math (same model as playbook.py) ─────────────────────────────

def _exp_spend_delta_7d(budget: float, rec_budget: float, sb: float) -> float:
    if rec_budget > budget:
        return (rec_budget - budget) * sb * 0.88 * 7
    elif rec_budget < budget:
        old_pacing = budget * min(sb, 1.0)
        hard = min(rec_budget, old_pacing) - old_pacing
        soft = (rec_budget - budget) * sb
        return min(hard, soft) * 7
    return 0.0


def _released(budget, rec_budget, sb):
    if rec_budget >= budget:
        return 0.0
    daily = budget * min(sb, 1.0)
    return max(0.0, (daily - min(rec_budget, daily)) * 7)


def _absorbed(budget, rec_budget, sb):
    if rec_budget <= budget:
        return 0.0
    return (rec_budget - budget) * sb * 0.88 * 7


# ── prompt builder ────────────────────────────────────────────────────────────

SYSTEM = """\
You are a Google Ads budget optimization expert for ShopDeck (Indian e-commerce).

GOAL: Maximize total profitable spend across campaigns for each account.

METRIC — S/GMV = Cost ÷ Conversion Value. LOWER S/GMV = BETTER (more profitable).
  CRITICAL: A campaign at 15% S/GMV is FAR BETTER than one at 23% S/GMV.
  Always scale campaigns with the LOWEST S/GMV first. Never scale a high-S/GMV campaign over a low-S/GMV one.

Two S/GMV types — never mix them:
  • wtd_sgmv = true WTD S/GMV from back-end data → compare ONLY against be5 (actual 5% BE)
  • sg (per campaign) = dashboard S/GMV from Google Ads API → compare ONLY against db5/db0

Thresholds:
  • be5 = actual S/GMV breakeven at 5% margin. wtd_sgmv < be5 → account is profitable.
  • db5 = dashboard-adjusted 5% BE. sg < db5 → campaign is profitable.
  • db0 = dashboard-adjusted 0% BE. sg > db0 → campaign in loss.
  • S/B = Spend÷Budget (3-day). High S/B → budget is the binding constraint → scaling will increase spend.
  • isb = IS lost to budget (direct scaling opportunity). isr = IS lost to rank (bid/quality — budget alone won't help).

KEY INSIGHT: Even if ALL campaigns are in loss, move spend from high-S/GMV (worse) to low-S/GMV (better) campaigns. This always improves overall ROAS and PnL.

Read campaign names carefully — they often contain critical context ("Do Not Touch", "Maxed Out", dates, A/B variants). Use your judgment on every case.

MODES:
  • Rebalance (neutral): spend-neutral reallocation. Do NOT scale campaigns just because an account is underspending — only scale if spend released from cuts can fund it.
  • RevOps/UNDER: aggressive spend recovery. MANDATORY rules:
      - Every campaign with sg < db5 → action="scale", rec_budget = budget × 1.40-1.50 (push hard).
      - Every campaign with db5 ≤ sg < db0 → action="scale", rec_budget = budget × 1.15-1.25.
      - Campaigns with sg > db0 or no conversions → action="cut" or "leave".
      - EXCEPTION: never scale campaigns whose name contains "DNT", "Do Not Touch", or "Maxed Out".
      - Post-processing enforces the 50% cap — set recs aggressively, guardrail will clip if needed.
  • RevOps/OVER: cut aggressively to reduce spend to target.

Guardrails (enforced in code after your output):
  1. In Rebalance mode: total spend must stay roughly neutral for under/on-target accounts.
  2. Max single-campaign increase: 50% above current budget.

Output via tool call: for each account provide a recommended daily budget for every campaign + a 1-2 sentence summary of your strategy.\
"""


def _pnl_str(pnl: list) -> str:
    m = {1: "P", 2: "B", 3: "L", 4: "-"}
    if not pnl or len(pnl) < 7:
        return "?"
    return f"{m.get(pnl[0],'?')},{m.get(pnl[1],'?')},{m.get(pnl[6],'?')}"   # LLW,LW,WTD


def _build_batch_prompt(pairs: list[tuple], mode: str = "neutral") -> str:
    """pairs: list of (account_dict, campaigns_list)"""
    mode_line = f"MODE: {'RevOps' if mode == 'revops' else 'Rebalance (neutral)'}"
    lines = [f"{mode_line}\nAnalyze each account and call submit_all_recommendations:\n"]
    for account, camps in pairs:
        db5  = account.get("db5")
        db0  = account.get("db0")
        be5  = account.get("be5")
        db5s = f"{db5*100:.1f}%" if db5 else "?"
        db0s = f"{db0*100:.1f}%" if db0 else "?"
        be5s = f"{be5*100:.1f}%" if be5 else "?"
        wtds = f"{account['wtdSGmv']*100:.1f}%" if account.get("wtdSGmv") else "?"
        spv  = (account.get("spv") or "?").upper()
        tgt  = int(account.get("target") or 0)
        pnl  = _pnl_str(account.get("pnl") or [])

        lines.append(
            f"=== {account['id']} | {account['name']} | "
            f"wtd_sgmv:{wtds} be5:{be5s} db5:{db5s} db0:{db0s} | {spv} | tgt:₹{tgt:,} | PnL:{pnl} ==="
        )
        sorted_c = sorted(camps, key=lambda c: c["sgmv"] if c["sgmv"] else 999)
        for c in sorted_c:
            sg  = f"{c['sgmv']*100:.1f}%" if c["sgmv"] else "NOCONV"
            isb = f"{c['is_budget']*100:.0f}%" if c.get("is_budget") else "—"
            isr = f"{c['is_rank']*100:.0f}%"   if c.get("is_rank")   else "—"
            name = c["name"][:45].replace("|", "/")
            lines.append(
                f"  [{c['id']}] {name} | {c['type']} | "
                f"sg:{sg} sb:{c['sb']*100:.0f}% isb:{isb} isr:{isr} | "
                f"bud:₹{int(c['budget']):,} cost7d:₹{int(c['cost7d']):,}"
            )
        lines.append("")
    return "\n".join(lines)


# ── tool schema ───────────────────────────────────────────────────────────────

TOOL = {
    "name": "submit_all_recommendations",
    "description": "Submit budget recommendations for all accounts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "accounts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":       {"type": "string", "description": "Account seller ID"},
                        "summary":  {"type": "string", "description": "1-2 sentence strategy summary"},
                        "signals": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "c": {"type": "string", "enum": ["green","amber","red","blue","grey"]},
                                    "t": {"type": "string"}
                                },
                                "required": ["c","t"]
                            }
                        },
                        "campaigns": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id":         {"type": "string"},
                                    "rec_budget": {"type": "number", "description": "Recommended daily budget in INR"},
                                    "action":     {"type": "string", "enum": ["scale","cut","watch","leave"]}
                                },
                                "required": ["id","rec_budget","action"]
                            }
                        }
                    },
                    "required": ["id","summary","campaigns","signals"]
                }
            }
        },
        "required": ["accounts"]
    }
}


# ── post-processing ───────────────────────────────────────────────────────────

def _fmt(n: float) -> str:
    return "₹" + f"{int(round(n)):,}"


_DNT_KEYWORDS = ("DNT", "DO NOT TOUCH", "MAXED OUT", "PAUSED")


def _is_dnt(name: str) -> bool:
    n = name.upper()
    return any(k in n for k in _DNT_KEYWORDS)


def _post_process_account(account: dict, campaigns: list[dict],
                           llm_acct: dict, mode: str = "neutral") -> dict | None:
    """Apply hard guardrails on top of Claude's budget recommendations."""
    db5 = account.get("db5")
    db0 = account.get("db0")
    spv = account.get("spv", "on")
    revops_under = (mode == "revops" and spv == "under")

    # Build campaign lookup
    llm_by_id = {r["id"]: r for r in llm_acct.get("campaigns", [])}

    # Merge: LLM rec → original campaign data
    merged = []
    for c in campaigns:
        llm = llm_by_id.get(c["id"])
        if not llm:
            merged.append({**c, "rec_budget": c["budget"], "action": "leave"})
            continue
        rec = float(llm["rec_budget"])
        rec = max(rec, 100.0)
        rec = min(rec, c["budget"] * 1.50)   # guardrail 2
        merged.append({**c, "rec_budget": round(rec, 0), "action": llm["action"]})

    # RevOps+UNDER: force-scale ALL profitable campaigns the LLM left behind
    if revops_under:
        for c in merged:
            if _is_dnt(c.get("name", "")):
                continue
            sgmv = c.get("sgmv")
            # Profit campaigns (sg < db5) → push to 50%
            if sgmv is not None and db5 is not None and sgmv < db5:
                if c["action"] in ("leave", "watch"):
                    c["action"]     = "scale"
                    c["rec_budget"] = round(c["budget"] * 1.50, 0)
                elif c["action"] == "scale" and c["rec_budget"] < c["budget"] * 1.30:
                    c["rec_budget"] = round(c["budget"] * 1.50, 0)
            # Border campaigns (db5 ≤ sg < db0) → modest 20% lift
            elif (sgmv is not None and db5 is not None and db0 is not None
                  and db5 <= sgmv < db0 and c["action"] in ("leave", "watch")):
                c["action"]     = "scale"
                c["rec_budget"] = round(c["budget"] * 1.20, 0)

    # Spend transfer balance (skip in RevOps underspend — goal is to add spend)
    released = sum(_released(c["budget"], c["rec_budget"], c["sb"])
                   for c in merged if c["action"] == "cut")
    absorbed = sum(_absorbed(c["budget"], c["rec_budget"], c["sb"])
                   for c in merged if c["action"] == "scale")

    # Spend-neutral: pull back scales proportionally if absorbed > released.
    # Applies universally in neutral mode; RevOps/under is exempt.
    if not revops_under and absorbed > released + 100:
        if released <= 0:
            for c in merged:
                if c["action"] == "scale":
                    c["action"]     = "leave"
                    c["rec_budget"] = c["budget"]
        else:
            adj = released / absorbed
            for c in merged:
                if c["action"] == "scale":
                    excess = c["rec_budget"] - c["budget"]
                    c["rec_budget"] = max(round(c["budget"] + excess * adj, 0), 100.0)

    # Format output campaigns
    result_campaigns = []
    for c in merged:
        delta   = c["rec_budget"] - c["budget"]
        pct_ch  = abs(delta) / c["budget"] if c["budget"] > 0 else 0
        # Bypass 8% filter for RevOps scale actions
        if pct_ch < 0.08 and not (revops_under and c["action"] == "scale"):
            c["action"]     = "watch" if c["action"] == "watch" else "leave"
            c["rec_budget"] = c["budget"]
            delta = 0.0

        dir_        = "p" if delta > 0 else ("n" if delta < 0 else "f")
        delta_s     = ("+" if delta > 0 else "") + _fmt(delta) if delta != 0 else "₹0"
        spend_d7    = _exp_spend_delta_7d(c["budget"], c["rec_budget"], c["sb"])

        result_campaigns.append({
            "id":                c["id"],
            "name":              c["name"],
            "type":              c["type"],
            "roas":              c["roas"],
            "sgmv":              c["sgmv"],
            "sb":                c["sb"],
            "cost7d":            c["cost7d"],
            "budg":              _fmt(c["budget"]),
            "rec":               _fmt(c["rec_budget"]),
            "dir":               dir_,
            "delta":             delta_s,
            "sbPct":             f"{round(c['sb']*100)}%",
            "bucket":            c["action"],
            "expSpendDeltaDay":  round(spend_d7 / 7, 0),
        })

    has_action = any(c["bucket"] in ("scale","cut") for c in result_campaigns)
    if not has_action and not llm_acct.get("signals"):
        return None

    return {
        "campaigns":     result_campaigns,
        "signals":       llm_acct.get("signals", []),
        "projRoas":      "—",
        "flag":          "",
        "recentChanges": llm_acct.get("summary", ""),
    }


# ── main entry point ──────────────────────────────────────────────────────────

def rebalance_all(pairs: list[tuple], mode: str = "neutral") -> dict:
    """
    pairs: list of (account_dict, campaigns_list)
    Returns: {seller_id: rec_dict}
    """
    if not pairs:
        return {}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Split into batches
    batches = [pairs[i:i+BATCH_SIZE] for i in range(0, len(pairs), BATCH_SIZE)]

    # For each batch: one API call (run sequentially; parallelism via thread pool in main.py)
    all_results = {}
    for batch in batches:
        prompt = _build_batch_prompt(batch, mode=mode)
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "submit_all_recommendations"},
                messages=[{"role": "user", "content": prompt}]
            )
            for block in msg.content:
                if block.type == "tool_use" and block.name == "submit_all_recommendations":
                    acct_map = {a["id"]: (a, c) for a, c in batch}
                    for llm_acct in block.input.get("accounts", []):
                        sid = llm_acct.get("id")
                        if sid not in acct_map:
                            continue
                        account, campaigns = acct_map[sid]
                        rec = _post_process_account(account, campaigns, llm_acct, mode=mode)
                        if rec:
                            all_results[sid] = rec
        except Exception as e:
            print(f"LLM batch error: {e}")
            # Fallback handled in main.py

    return all_results
