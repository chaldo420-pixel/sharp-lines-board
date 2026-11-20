#!/usr/bin/env python3
"""Build the public Sharp Lines board (board.json) from PropLine.

Runs in GitHub Actions (see .github/workflows/refresh.yml) every
15 minutes. Produces board.json in the exact slim format the
public page consumes (same as slim_board.py output), plus the
daily PropLine call counter in requests_log.jsonl.

Rules (same model as the Sharp Lines Dashboard):
  * Game lines: DraftKings + FanDuel vs the Pinnacle no-vig
    fair price, at the SAME line/handicap only. A >= 0.5
    handicap difference flags the book "line-differs", EV None.
  * Props: straight main over/under lines only (no alt lines,
    no GemFinder). Anchor = consensus no-vig fair price across
    the OTHER books at the same line, sharp books
    (BookMaker, Circa, Kalshi, Polymarket, Novig) double
    weighted. BetMGM, BetRivers and Fanatics are fallback-only:
    dropped from the anchor whenever 2+ sharp books price the
    line, kept only to reach the 2-book minimum when sharp
    coverage is thin (see props.py). A prop row's +EV flag
    comes from its BEST book; "+EV >=2%" only ever appears on
    a sharp anchor.
  * Alert threshold +2% EV. No bet links anywhere.
  * Window: 08:00-23:00 ET only. Outside the window the run
    exits quietly (status SKIPPED), no API calls.
  * Budget: PropLine free tier, 1,000 requests/day. The run
    stops for the day once today's counted calls would pass
    850 (counter in requests_log.jsonl, committed each run).

The PropLine key comes ONLY from the PROPLINE_API_KEY
environment variable (the repo secret in CI). It is never
logged, never written to a file. If it is missing the run
fails loudly (exit 3) — a silent empty board is worse than
a failed run. No bets are placed anywhere in this file.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_propline import (  # noqa: E402
    _odds_for_event, list_events, parse_game_lines, parse_props)
from poll import (  # noqa: E402
    ALERT_THRESHOLD_EV, BUDGET_CAP, BUDGET_ESTIMATE_CALLS,
    in_window, run_game_rows, run_prop_rows, today_total)
from slim_board import slim_row  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
ET = ZoneInfo("America/New_York")


def _board_path():
    return os.path.join(REPO_ROOT, "board.json")


def _log_path():
    return os.path.join(REPO_ROOT, "requests_log.jsonl")


def _log_calls(date_str, calls, note=""):
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date_str, "ts": datetime.now(ET).isoformat(),
            "calls": int(calls), "mode": "pages",
            "note": note,
        }) + "\n")


def _best_book(dk_ev, fd_ev):
    """(best_book, best_ev): better of the two EVs; DK wins ties."""
    dk = None if dk_ev in (None, "") else float(dk_ev)
    fd = None if fd_ev in (None, "") else float(fd_ev)
    if dk is None and fd is None:
        return None, None
    if fd is None or (dk is not None and dk >= fd):
        return "DraftKings", dk
    return "FanDuel", fd


def _round_american(value):
    """American odds as an int (PropLine prices are integral)."""
    if value in (None, ""):
        return None
    return int(round(float(value)))


def _game_dash_row(r, game_date):
    """One poll.py game row -> dashboard-shaped row.

    flag is the DraftKings flag (the dashboard's row flag);
    fdFlag is FanDuel's own. line is the Pinnacle handicap
    for totals only, else null (moneyline/spread carry the
    handicap in the side string).
    """
    best_book, best_ev = _best_book(r.get("ev_pct"),
                                    r.get("fanduel_ev_pct"))
    return {
        "game": r["game"],
        "gameDate": game_date,
        "kind": "game",
        "player": None,
        "market": r["market"],
        "side": r["side"],
        "line": (r.get("pinnacle_handicap")
                 if r.get("market") == "total" else None),
        "dkAmerican": _round_american(r.get("draftkings_american")),
        "fdAmerican": _round_american(r.get("fanduel_american")),
        "evPct": r.get("ev_pct"),
        "fdEvPct": r.get("fanduel_ev_pct"),
        "bestBook": best_book,
        "bestEvPct": best_ev,
        "fairProb": r.get("pinnacle_fair_prob"),
        "anchor": "pinnacle-novig",
        "flag": r.get("flag"),
    }


def _prop_dash_row(r, game_date):
    """One poll.py prop row -> dashboard-shaped row.

    flag is the DraftKings flag (sharp-gated '+EV >=2%',
    parenthetical '+EV (soft consensus — do not alert)' /
    '+EV (single-book anchor — do not alert)' on non-sharp
    anchors); fdFlag is FanDuel's own, without parenthetical.
    """
    best_book, best_ev = _best_book(r.get("ev_pct"),
                                    r.get("fanduel_ev_pct"))
    return {
        "game": r["game"],
        "gameDate": game_date,
        "kind": "prop",
        "player": r["player"],
        "market": r["market"],
        "side": r["side"],
        "line": r.get("line"),
        "dkAmerican": _round_american(r.get("draftkings_american")),
        "fdAmerican": _round_american(r.get("fanduel_american")),
        "evPct": r.get("ev_pct"),
        "fdEvPct": r.get("fanduel_ev_pct"),
        "bestBook": best_book,
        "bestEvPct": best_ev,
        "fairProb": r.get("fair_prob"),
        "anchor": r.get("anchor"),
        "anchorDisplay": r.get("anchor_display"),
        "flag": r.get("flag"),
    }


def _sort_key(row):
    """Best EV descending; rows with no EV go last."""
    ev = row.get("bestEvPct")
    return (ev is None, -(ev if ev is not None else 0.0))


def build(now_et):
    """One refresh. Returns (status, calls)."""
    date_str = now_et.strftime("%Y-%m-%d")
    ts = now_et.strftime("%Y-%m-%d %H:%M ET")
    label = now_et.strftime("PropLine live · %b %-d, %-I:%M %p ET")
    source = f"PropLine live {ts}"
    counter = [0]

    if not os.path.exists(_log_path()):
        open(_log_path(), "a", encoding="utf-8").close()

    if not budget_ok(date_str, BUDGET_ESTIMATE_CALLS):
        print(f"BUDGET_EXCEEDED: today {today_total(date_str, _log_path())} "
              f"calls + ~{BUDGET_ESTIMATE_CALLS} estimated > cap "
              f"{BUDGET_CAP}; no refresh, no API calls")
        return "BUDGET_EXCEEDED", 0

    events = list_events(counter)
    game_dates = {}
    for ev in events:
        game_label = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        ct = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            game_dates[game_label] = dt.astimezone(ET).date().isoformat()
        except (ValueError, AttributeError):
            game_dates[game_label] = date_str

    if not budget_ok(date_str, len(events)):
        print(f"BUDGET_EXCEEDED: today {today_total(date_str, _log_path())} "
              f"calls + {len(events)} event calls > cap {BUDGET_CAP}; "
              f"stopped before odds calls")
        _log_calls(date_str, counter[0], note="events list only, "
                   "stopped on budget")
        return "BUDGET_EXCEEDED", counter[0]

    raw_game, raw_props = [], []
    for ev in events:
        game_label = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        payload = _odds_for_event(ev.get("id"), counter)
        raw_game.extend(parse_game_lines(payload, game_label))
        raw_props.extend(parse_props(payload, game_label))

    _log_calls(date_str, counter[0],
               note=f"{len(events)} events, {len(raw_game)} game rows, "
                    f"{len(raw_props)} prop groups")

    game_rows = run_game_rows(raw_game, ts, source)
    prop_rows = run_prop_rows(raw_props, ts, source)

    dash_rows = []
    for r in game_rows:
        dash_rows.append(_game_dash_row(
            r, game_dates.get(r["game"], date_str)))
    for r in prop_rows:
        dash_rows.append(_prop_dash_row(
            r, game_dates.get(r["game"], date_str)))
    dash_rows.sort(key=_sort_key)

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "snapshotLabel": label,
        "snapshotAt": now_et.isoformat(),
        "rowCount": len(dash_rows),
        "thresholdPct": int(ALERT_THRESHOLD_EV),
        "rows": [slim_row(r) for r in dash_rows],
    }
    with open(_board_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))

    print(f"LIVE calls={counter[0]} "
          f"today_total={today_total(date_str, _log_path())} "
          f"events={len(events)} game_rows={len(game_rows)} "
          f"prop_rows={len(prop_rows)} board_rows={len(dash_rows)}")
    return "LIVE", counter[0]


def budget_ok(date_str, estimated_calls):
    return (today_total(date_str, _log_path()) + int(estimated_calls)
            <= BUDGET_CAP)


def main():
    if not os.environ.get("PROPLINE_API_KEY"):
        print("ERROR: PROPLINE_API_KEY is not set. Add it as a repo "
              "secret (Settings -> Secrets and variables -> Actions) "
              "and re-run. No API calls made.")
        return 3
    now_et = datetime.now(ET)
    if not in_window(now_et):
        print(f"SKIPPED: {now_et.strftime('%H:%M')} ET is outside the "
              f"08:00-23:00 ET window — no API calls, no refresh")
        return 0
    status, _calls = build(now_et)
    return 0 if status in ("LIVE", "BUDGET_EXCEEDED") else 3


if __name__ == "__main__":
    raise SystemExit(main())
