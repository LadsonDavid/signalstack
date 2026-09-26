"""Two-factor scoring, computed at READ time.

    fit    = static "does this look like a Malveon customer",  0-100, no decay
    intent = perishable "are they in market right now",        0-100, decayed
    score  = fit/100 * intent

Fit multiplies rather than adds, because for a pre-launch product a screaming
signal from a non-ICP company is worse than useless — it costs you the one thing
you cannot buy back, which is outreach time.

Nothing here is persisted. The .docx proposes cron jobs that periodically
decay stored point values; that is mutable derived state and it drifts. Instead
every score is recomputed from the append-only signal log on demand, so editing
a half-life in malveon.yaml correctly re-scores all of history immediately.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .config import cfg

def trigger_kinds() -> set[str]:
    return set(cfg()["lead_types"]["company_triggers"])


def intent_kinds() -> set[str]:
    return set(cfg()["lead_types"]["intent_signals"])


def _days_old(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)


def decay(weight: float, age_days: float, half_life: float) -> float:
    return weight * (0.5 ** (age_days / half_life)) if half_life > 0 else weight


def score_rows(rows, headcount: int | None = None) -> dict:
    """Score one account from its signal rows. `rows` are sqlite3.Row or dicts."""
    c = cfg()
    F, I = c["fit"], c["intent"]

    tools: dict[str, str] = {}
    flags: dict[str, str] = {}
    personas: set[str] = set()
    reasons: list[dict] = []
    per_kind: dict[str, float] = {}

    for r in rows:
        kind = r["kind"]
        if kind in ("persona_pain_match", "persona_pain_post"):
            try:
                p = json.loads(r["payload"] or "{}").get("persona")
                if p:
                    personas.add(p)
            except Exception:
                pass
        if kind == "integration_detected":
            # Read the tool from the payload, never by splitting the detail
            # string — that silently turns "Google Docs" into "Google".
            tool = None
            raw = r["payload"] if "payload" in r.keys() else None
            if raw:
                try:
                    tool = json.loads(raw).get("tool")
                except Exception:
                    tool = None
            if not tool:
                tool = (r["detail"] or "").split(" (")[0].replace(" named in job posts", "")
            tools.setdefault(tool, r["detail"] or tool)
        elif kind in ("status_page", "owns_production_language", "persona_pain_match",
                      "competitor_complementary", "competitor_displacement"):
            flags.setdefault(kind, r["detail"] or kind)
        elif kind in I:
            spec = I[kind]
            pts = decay(spec["weight"], _days_old(r["observed_at"]), spec["half_life_days"])
            if pts >= 0.5:
                per_kind[kind] = per_kind.get(kind, 0.0) + pts
                reasons.append({"axis": "intent", "kind": kind, "points": round(pts, 1),
                                "detail": r["detail"] or kind, "url": r["url"] or "",
                                "when": (r["observed_at"] or "")[:10]})

    # ---- fit -----------------------------------------------------------
    # Integrations are weighted, not counted. Every dev-infra company runs
    # AWS/Kubernetes/GitHub, so counting them flattens the whole board to fit=100.
    # The tools that actually predict a Malveon buyer are the coordination and
    # production-ownership ones: Linear, Jira, Notion, Slack, PagerDuty, Sentry.
    fit = 0.0
    if tools:
        spec = F["integration_detected"]
        weights = spec.get("weights", {})
        default = spec.get("each", 8)
        pts = min(sum(weights.get(t, default) for t in tools), spec["cap"])
        top = sorted(tools, key=lambda t: -weights.get(t, default))
        fit += pts
        reasons.append({"axis": "fit", "kind": "integration_detected", "points": round(pts, 1),
                        "detail": f"Uses {len(tools)} tools Malveon connects to: "
                                  + ", ".join(top[:4]) + (f" and {len(tools)-4} more" if len(tools) > 4 else ""),
                        "url": "", "when": ""})
    # Tools grouped by which Malveon product they justify pitching. Sprawl
    # ACROSS lines is the condition Malveon exists for, so it is worth more
    # than the same number of tools inside a single category.
    lines: dict[str, list[str]] = {}
    for t in tools:
        ln = (c["fingerprints"].get(t) or {}).get("line")
        if ln:
            lines.setdefault(ln, []).append(t)
    if len(lines) >= 2:
        mb = F["multi_line_bonus"]
        pts = mb["three_lines"] if len(lines) >= 3 else mb["two_lines"]
        fit += pts
        labels = [c["product_lines"][k]["label"].split(" — ")[0] for k in lines]
        reasons.append({"axis": "fit", "kind": "multi_line", "points": pts,
                        "detail": f"Their tools are spread across {len(lines)} areas Malveon covers: "
                                  + ", ".join(sorted(labels)),
                        "url": "", "when": ""})

    for kind in ("status_page", "owns_production_language", "persona_pain_match",
                 "competitor_complementary", "competitor_displacement"):
        if kind in flags:
            fit += F[kind]
            reasons.append({"axis": "fit", "kind": kind, "points": F[kind],
                            "detail": flags[kind], "url": "", "when": ""})
    band = F["headcount_band"]
    if headcount and band["min"] <= headcount <= band["max"]:
        fit += band["points"]
        reasons.append({"axis": "fit", "kind": "headcount_band", "points": band["points"],
                        "detail": f"{headcount} people — the right size for Malveon", "url": "", "when": ""})

    # A repeated weak signal must never outweigh one real trigger. Four months
    # of "we're hiring" posts is context; a first-SRE-hire is a buying moment.
    intent_total = sum(min(v, I[k].get("max", 1e9)) for k, v in per_kind.items())

    # No tracked competitor spans all three product lines (context/planning/
    # execution) — that breadth is the whole thesis. A company hurting in two
    # or more lines AT ONCE is exactly the account only Malveon fits, so it is
    # worth more than the sum of those signals scored separately.
    pain_lines = {c["personas"][p]["line"] for p in personas if p in c["personas"]}
    if per_kind.keys() & {"major_incident", "recent_incident", "incident_streak",
                          "new_status_page", "first_sre_hire", "first_release_hire"}:
        pain_lines.add("malviont")
    if len(pain_lines) >= 2:
        mb = c["multi_layer_pain_bonus"]
        bonus = mb["three_lines"] if len(pain_lines) >= 3 else mb["two_lines"]
        intent_total += bonus
        line_labels = [c["product_lines"][l]["label"].split(" — ")[0] for l in pain_lines]
        reasons.append({"axis": "intent", "kind": "multi_layer_pain", "points": bonus,
                        "detail": f"Hurting in {len(pain_lines)} areas at once, not just one: "
                                  + ", ".join(sorted(line_labels)), "url": "", "when": ""})

    fit = max(0.0, min(100.0, fit))
    intent = max(0.0, min(100.0, intent_total))
    reasons.sort(key=lambda x: -x["points"])

    # Fit gates but does not annihilate — an account discovered from a hot post
    # before techstack ever ran has fit 0 through no fault of its own.
    floor = c["thresholds"]["fit_floor"]
    w = F["integration_detected"].get("weights", {})
    default = F["integration_detected"].get("each", 8)
    # The line with the most detected tools is the product to lead the pitch with.
    best_line = max(lines, key=lambda k: len(lines[k])) if lines else None
    return {
        "fit": round(fit), "intent": round(intent),
        "score": round((floor + (1 - floor) * fit / 100) * intent),
        "tools": sorted(tools),
        # Ranked by how much each tool actually predicts a Malveon buyer, so the
        # suggested opening line leads with Jira/PagerDuty, not AWS.
        "tools_ranked": sorted(tools, key=lambda t: (-w.get(t, default), t)),
        "lines": {k: sorted(v) for k, v in lines.items()},
        "best_line": best_line,
        "best_line_label": c["product_lines"][best_line]["label"] if best_line else "",
        "best_line_hook": c["product_lines"][best_line]["hook"] if best_line else "",
        "personas": sorted(personas),
        "reasons": reasons,
        "kinds": {r["kind"] for r in rows},
        "last_signal": max((r["observed_at"] or "" for r in rows), default=""),
    }


def score_all(only_ids: list[int] | None = None) -> dict[int, dict]:
    """One pass over the signal log for every account. Cheap at this scale."""
    c = db.conn()
    by_acct: dict[int, list] = defaultdict(list)
    q = "SELECT account_id,kind,detail,url,value,payload,observed_at FROM signal"
    params: tuple = ()
    if only_ids:
        q += f" WHERE account_id IN ({','.join('?' * len(only_ids))})"
        params = tuple(only_ids)
    for r in c.execute(q, params):
        by_acct[r["account_id"]].append(r)

    heads = {r["id"]: r["headcount"] for r in c.execute("SELECT id,headcount FROM account")}
    return {aid: score_rows(rows, heads.get(aid)) for aid, rows in by_acct.items()}


def tier(score: int) -> str:
    t = cfg()["thresholds"]
    if score >= t["sql"]:
        return "hot"
    if score >= t["mql"]:
        return "warm"
    return "cold"


def recent_trigger(rows, window_days: int) -> dict | None:
    """Most recent in-window trigger event, or None."""
    best = None
    kinds = trigger_kinds()
    for r in rows:
        if r["kind"] in kinds and _days_old(r["observed_at"]) <= window_days:
            if best is None or (r["observed_at"] or "") > (best["observed_at"] or ""):
                best = r
    return best


# How specific a reason is, independent of how many points it scores.
# "3 Malveon integrations, led by Slack, AWS" is worth more points than
# "hiring Senior SRE" but is far less useful to open an email with, so the
# board must lead with evidence you can quote back to a human, not aggregates.
SPECIFICITY = {
    "stated_intent": 0, "competitor_gripe": 0, "persona_pain_post": 0,
    "major_incident": 1, "incident_streak": 1, "recent_incident": 1, "multi_layer_pain": 1,
    "site_visit_pricing": 1, "site_visit": 1,
    "persona_pain_match": 2, "competitor_evaluation": 2,
    "first_sre_hire": 3, "first_em_hire": 3, "first_release_hire": 3,
    "first_security_hire": 3, "funding_seed_a": 3,
    "competitor_displacement": 4, "competitor_complementary": 4,
    "hiring_publicly": 5, "eng_req_surge": 5, "new_status_page": 5,
    "owns_production_language": 6, "status_page": 6,
    "headcount_band": 7,
    "integration_detected": 8, "multi_line": 8,
    "active_research": 9, "discovered": 9,
}


def explain(sc: dict, limit: int = 3, skip: str | None = None) -> str:
    """One-line 'why is this here', led by the most quotable evidence.

    `skip` drops a detail already shown elsewhere on the row — the board
    surfaces the newest trigger on its own line, and repeating it verbatim as
    the first reason wastes the most valuable space on the page.
    """
    if not sc["reasons"]:
        return "nothing found yet"
    ranked = sorted(sc["reasons"],
                    key=lambda r: (SPECIFICITY.get(r["kind"], 5), -r["points"]))
    seen = {skip} if skip else set()
    out = []
    for r in ranked:
        if r["detail"] in seen:
            continue
        seen.add(r["detail"])
        out.append(r["detail"])
        if len(out) == limit:
            break
    return " · ".join(out)


__all__ = ["score_all", "score_rows", "tier", "explain", "decay", "recent_trigger",
           "trigger_kinds", "intent_kinds"]
