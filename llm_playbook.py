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
        daily = budget * min(sb, 1.0)
        new_daily = min(rec_budget, daily)
        return (new_daily - daily) * 7
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
METRIC: S/GMV = Cost ÷ Conversion Value. Lower = better (more profitable).
  • db5 = S/GMV threshold for 5% profit margin. Campaign profitable if S/GMV < db5.
  • db0 = S/GMV threshold for breakeven. Campaign in loss if S/GMV > db0.
  • S/B = Spend÷Budget ratio. High S/B → budget is the constraint, scaling will increase spend.
  • isb = IS lost to budget (direct scaling opportunity). isr = IS lost to rank (bid/quality issue — budget alone won't help).

KEY INSIGHT: Even if ALL campaigns are in loss, moving spend from high-loss campaigns to low-loss campaigns improves overall ROAS and PnL. Always reallocate from worst to best performers.

Read campaign names carefully — they often contain critical context ("Do Not Touch", "Maxed Out", dates, A/B variants). Use your judgment on every case.

Guardrails (enforced in code after your output — keep in mind):
  1. Total spend must stay roughly neutral for under/on-target accounts.
  2. Max single-campaign increase: 50% above current budget.

Output via tool call: for each account provide a recommended daily budget for every campaign + a 1-2 sentence summary of your strategy.\
"""


def _pnl_str(pnl: list) -> str:
    m = {1: "P", 2: "B", 3: "L", 4: "-"}
    if not pnl or len(pnl) < 7:
        return "?"
    return f"{m.get(pnl[0],'?')},{m.get(pnl[1],'?')},{m.get(pnl[6],'?')}"   # LLW,LW,WTD


def _build_batch_prompt(pairs: list[tuple]) -> str:
    """pairs: list of (account_dict, campaigns_list)"""
    lines = ["Analyze each account and call submit_all_recommendations:\n"]
    for account, camps in pairs:
        db5  = account.get("db5")
        db0  = account.get("db0")
        db5s = f"{db5*100:.1f}%" if db5 else "?"
        db0s = f"{db0*100:.1f}%" if db0 else "?"
        wtds = f"{account['wtdSGmv']*100:.1f}%" if account.get("wtdSGmv") else "?"
        spv  = (account.get("spv") or "?").upper()
        tgt  = int(account.get("target") or 0)
        pnl  = _pnl_str(account.get("pnl") or [])

        lines.append(
            f"=== {account['id']} | {account['name']} | "
            f"wtd:{wtds} db5:{db5s} db0:{db0s} | {spv} | tgt:₹{tgt:,} | PnL:{pnl} ==="
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


def _post_process_account(account: dict, campaigns: list[dict],
                           llm_acct: dict) -> dict | None:
    """Apply hard guardrails on top of Claude's budget recommendations."""
    db5 = account.get("db5")
    db0 = account.get("db0")
    spv = account.get("spv", "on")

    # Build campaign lookup
    camp_by_id = {c["id"]: c for c in campaigns}
    llm_by_id  = {r["id"]: r for r in llm_acct.get("campaigns", [])}

    # Merge: LLM rec → original campaign data
    merged = []
    for c in campaigns:
        llm = llm_by_id.get(c["id"])
        if not llm:
            merged.append({**c, "rec_budget": c["budget"], "action": "leave"})
            continue
        rec = float(llm["rec_budget"])
        # Hard caps
        rec = max(rec, 100.0)
        rec = min(rec, c["budget"] * 1.50)   # guardrail 2
        merged.append({**c, "rec_budget": round(rec, 0), "action": llm["action"]})

    # Spend transfer balance
    released = sum(_released(c["budget"], c["rec_budget"], c["sb"])
                   for c in merged if c["action"] == "cut")
    absorbed = sum(_absorbed(c["budget"], c["rec_budget"], c["sb"])
                   for c in merged if c["action"] == "scale")

    if spv in ("under", "on") and released > absorbed + 100:
        if absorbed <= 0:
            for c in merged:
                if c["action"] == "cut":
                    c["action"]     = "leave"
                    c["rec_budget"] = c["budget"]
        else:
            ratio = absorbed / released
            for c in merged:
                if c["action"] == "cut":
                    excess = c["budget"] - c["rec_budget"]
                    c["rec_budget"] = max(round(c["budget"] - excess * ratio, 0), 100.0)

    # Format output campaigns
    result_campaigns = []
    for c in merged:
        delta   = c["rec_budget"] - c["budget"]
        pct_ch  = abs(delta) / c["budget"] if c["budget"] > 0 else 0
        if pct_ch < 0.08:
            c["action"]     = "watch" if c["action"] == "watch" else "leave"
            c["rec_budget"] = c["budget"]
            delta = 0.0

        dir_        = "p" if delta > 0 else ("n" if delta < 0 else "f")
        delta_s     = ("+" if delta > 0 else "") + _fmt(delta) if delta != 0 else "₹0"
        spend_d7    = _exp_spend_delta_7d(c["budget"], c["rec_budget"], c["sb"])

        result_campaigns.append({
            "id":             c["id"],
            "name":           c["name"],
            "type":           c["type"],
            "roas":           c["roas"],
            "sgmv":           c["sgmv"],
            "sb":             c["sb"],
            "cost7d":         c["cost7d"],
            "budg":           _fmt(c["budget"]),
            "rec":            _fmt(c["rec_budget"]),
            "dir":            dir_,
            "delta":          delta_s,
            "sbPct":          f"{round(c['sb']*100)}%",
            "bucket":         c["action"],
            "expSpendDelta7": round(spend_d7, 0),
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

def rebalance_all(pairs: list[tuple]) -> dict:
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
        prompt = _build_batch_prompt(batch)
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
                        rec = _post_process_account(account, campaigns, llm_acct)
                        if rec:
                            all_results[sid] = rec
        except Exception as e:
            print(f"LLM batch error: {e}")
            # Fallback handled in main.py

    return all_results
