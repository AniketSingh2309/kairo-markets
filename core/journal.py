"""Trade journal: paper fills -> round-trip trades -> the statistics that show what is working.

A trade opens when a position goes from flat to long or short, and closes when it is flat again (a flip
closes one trade and opens the next). P&L is before charges and taxes. When a trade began with a super
order, its initial stop gives the risk taken, so the result can be read as an R-multiple (+2R = won twice
what was risked). Notes and tags (setup, mistakes) come from the user.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SETUPS = ["Breakout", "Pullback", "Reversal", "Range trade", "Opening range", "VWAP", "News / results", "Kai's plan", "Other"]
MISTAKES = ["No stop-loss", "Moved my stop", "Chased the move", "Exited too early", "Too big a position", "Revenge trade",
            "Against the trend", "Traded on boredom", "Ignored my plan"]


def _at(v: Any) -> dt.datetime:
    return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(v)


def round_trips(fills: list[dict[str, Any]], init_stops: dict[int, float] | None = None) -> list[dict[str, Any]]:
    """fills: {id, order_id, at, symbol, side, qty, price, product}. init_stops: {entry order id: initial stop}."""
    init_stops = init_stops or {}
    by_line: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in sorted(fills, key=lambda f: (_at(f["at"]), f.get("id", 0))):
        by_line[(f["symbol"], f["product"])].append(f)
    trades: list[dict[str, Any]] = []
    for (sym, product), fs in by_line.items():
        pos, cur = 0.0, None

        def start(f, qty):
            return {"symbol": sym, "product": product, "side": "LONG" if f["side"] == "BUY" else "SHORT", "key": f"{sym}|{product}|{f.get('id', 0)}",
                    "opened_at": _at(f["at"]), "in_qty": qty, "in_value": qty * f["price"], "out_qty": 0.0, "out_value": 0.0,
                    "max_qty": qty, "fills": 1, "stop": init_stops.get(f.get("order_id"))}

        for f in fs:
            q = float(f["qty"])
            signed = q if f["side"] == "BUY" else -q
            if pos == 0:
                cur = start(f, q); pos = signed; continue
            if (pos > 0) == (signed > 0):                      # adding
                cur["in_qty"] += q; cur["in_value"] += q * f["price"]; cur["fills"] += 1
                pos += signed; cur["max_qty"] = max(cur["max_qty"], abs(pos)); continue
            closing = min(q, abs(pos))                          # reducing / closing / flipping
            cur["out_qty"] += closing; cur["out_value"] += closing * f["price"]; cur["fills"] += 1
            pos += signed if abs(signed) <= abs(pos) else (-pos)
            if abs(pos) < 1e-9:
                pos = 0.0
                cur["closed_at"] = _at(f["at"]); trades.append(cur); cur = None
                rest = q - closing
                if rest > 1e-9:                                 # flipped: the remainder opens the next trade
                    cur = start({**f, "id": f"{f.get('id', 0)}b"}, rest); pos = rest if f["side"] == "BUY" else -rest
        if cur is not None:
            cur["closed_at"] = None; trades.append(cur)
    out = []
    for t in trades:
        entry = t["in_value"] / t["in_qty"]
        exitp = t["out_value"] / t["out_qty"] if t["out_qty"] else None
        sign = 1 if t["side"] == "LONG" else -1
        pnl = (exitp - entry) * t["out_qty"] * sign if exitp is not None else None
        risk = abs(entry - t["stop"]) * t["out_qty"] if t["stop"] and t["out_qty"] else None
        closed = t["closed_at"] is not None
        out.append({"key": t["key"], "symbol": t["symbol"], "product": t["product"], "side": t["side"], "qty": round(t["max_qty"], 4),
                    "entry": round(entry, 4), "exit": round(exitp, 4) if exitp is not None else None, "status": "closed" if closed else "open",
                    "opened_at": t["opened_at"].isoformat(), "closed_at": t["closed_at"].isoformat() if closed else None,
                    "pnl": round(pnl, 2) if closed else None, "pnl_pct": round(pnl / (entry * t["out_qty"]) * 100, 2) if closed and entry else None,
                    "hold_minutes": round((t["closed_at"] - t["opened_at"]).total_seconds() / 60, 1) if closed else None,
                    "stop": t["stop"], "risk": round(risk, 2) if risk else None, "r": round(pnl / risk, 2) if closed and risk else None, "fills": t["fills"]})
    return sorted(out, key=lambda t: t["opened_at"], reverse=True)


def _group(trades: list[dict], key) -> list[dict[str, Any]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        for k in key(t):
            g[k].append(t)
    rows = [{"name": k, "trades": len(v), "win_rate": round(sum(1 for t in v if t["pnl"] > 0) / len(v) * 100, 1),
             "pnl": round(sum(t["pnl"] for t in v), 2), "avg": round(sum(t["pnl"] for t in v) / len(v), 2)} for k, v in g.items()]
    return sorted(rows, key=lambda r: r["pnl"], reverse=True)


def stats(trades: list[dict[str, Any]], notes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    notes = notes or {}
    closed = sorted([t for t in trades if t["status"] == "closed"], key=lambda t: t["closed_at"])
    if not closed:
        return {"trades": 0, "open": sum(1 for t in trades if t["status"] == "open")}
    wins, losses = [t for t in closed if t["pnl"] > 0], [t for t in closed if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    streak = worst_streak = 0
    for t in closed:
        eq += t["pnl"]; peak = max(peak, eq); dd = min(dd, eq - peak)
        curve.append({"at": t["closed_at"], "equity": round(eq, 2)})
        streak = streak + 1 if t["pnl"] <= 0 else 0; worst_streak = max(worst_streak, streak)
    rs = [t["r"] for t in closed if t["r"] is not None]
    hour = lambda t: [f"{dt.datetime.fromisoformat(t['opened_at']).astimezone(IST):%H}:00"]  # noqa: E731
    out = {
        "trades": len(closed), "open": sum(1 for t in trades if t["status"] == "open"), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1), "total": round(eq, 2), "expectancy": round(eq / len(closed), 2),
        "avg_win": round(gw / len(wins), 2) if wins else None, "avg_loss": round(-gl / len(losses), 2) if losses else None,
        "profit_factor": round(gw / gl, 2) if gl else None, "best": max(t["pnl"] for t in closed), "worst": min(t["pnl"] for t in closed),
        "max_drawdown": round(dd, 2), "worst_losing_streak": worst_streak,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "with_stop": len(rs),
        "avg_hold_win": round(sum(t["hold_minutes"] for t in wins) / len(wins), 1) if wins else None,
        "avg_hold_loss": round(sum(t["hold_minutes"] for t in losses) / len(losses), 1) if losses else None,
        "equity": curve,
        "by_setup": _group(closed, lambda t: [notes.get(t["key"], {}).get("setup") or "Untagged"]),
        "by_mistake": _group([t for t in closed if notes.get(t["key"], {}).get("mistakes")], lambda t: notes[t["key"]]["mistakes"]),
        "by_symbol": _group(closed, lambda t: [t["symbol"]])[:12],
        "by_hour": sorted(_group(closed, hour), key=lambda r: r["name"]),
        "by_side": _group(closed, lambda t: [t["side"]]),
    }
    out["insights"] = insights(out, closed)
    return out


def insights(s: dict[str, Any], closed: list[dict[str, Any]]) -> list[str]:
    """Plain-language observations a coach would make from these numbers."""
    tips = []
    n = s["trades"]
    if n < 10:
        tips.append(f"Only {n} closed trade{'s' if n != 1 else ''} so far: patterns get meaningful after about 20.")
    if s["avg_win"] and s["avg_loss"] and abs(s["avg_loss"]) > s["avg_win"]:
        tips.append(f"Your average loss (₹{abs(s['avg_loss']):,.0f}) is bigger than your average win (₹{s['avg_win']:,.0f}). "
                    f"With a {s['win_rate']:.0f}% win rate that {'still works' if s['expectancy'] > 0 else 'loses money'}; cutting losers sooner or letting winners run would help.")
    if s["avg_hold_win"] and s["avg_hold_loss"] and s["avg_hold_loss"] > 1.5 * s["avg_hold_win"]:
        tips.append("You hold losing trades much longer than winners: a classic sign of hoping a loser comes back. A stop-loss decides it for you.")
    no_stop = n - s["with_stop"]
    if n and no_stop / n > 0.5:
        tips.append(f"{no_stop} of {n} trades had no stop set in advance. Super orders attach one automatically, and let the journal measure results in R.")
    bad = [m for m in s["by_mistake"] if m["pnl"] < 0]
    if bad:
        tips.append(f"Trades tagged “{bad[-1]['name']}” cost you ₹{abs(bad[-1]['pnl']):,.0f} in total: that's the habit to work on first.")
    setups = [x for x in s["by_setup"] if x["name"] != "Untagged" and x["trades"] >= 3]
    if setups:
        best = setups[0]
        if best["pnl"] > 0:
            tips.append(f"Your best setup so far: {best['name']} ({best['trades']} trades, {best['win_rate']:.0f}% wins, ₹{best['pnl']:,.0f}).")
    hours = [h for h in s["by_hour"] if h["trades"] >= 3]
    if len(hours) >= 2:
        worst = min(hours, key=lambda h: h["pnl"])
        if worst["pnl"] < 0:
            tips.append(f"Trades opened around {worst['name']} have lost ₹{abs(worst['pnl']):,.0f}. Consider sitting that hour out.")
    if s["worst_losing_streak"] >= 4:
        tips.append(f"You've had a run of {s['worst_losing_streak']} losses in a row. A daily loss limit stops a bad day becoming a bad month.")
    untagged = next((x for x in s["by_setup"] if x["name"] == "Untagged"), None)
    if untagged and untagged["trades"] == n and n >= 3:
        tips.append("Tag each trade's setup: after a few weeks you'll see which ones actually make money for you.")
    return tips
