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
  * Adaptive cadence: the trigger fires every 5 minutes
    (cron-job.org); the model picks its own refresh interval
    from the distinct games on today's board snapshot, read
    from the already-checked-out board.json (zero PropLine
    calls to decide): 12+ games -> 20 min, 9-11 -> 15 min,
    5-8 -> 10 min, 4 or fewer -> 5 min. If the last snapshot
    is newer than the tier interval the run exits SKIPPED with
    no API calls and no board write. A missing snapshot, a
    snapshot from a previous day, or a 0-row snapshot is
    treated as unknown (never as "0 games") and runs a full
    refresh. The trigger may still be 15 minutes until the
    cron-job.org job is changed; until then every tick is due
    and the tier logic is harmless.
  * Flip marker: board.json carries flaggedToday, the lineKeys
    of every row that qualified (+EV >=2% on a sharp anchor)
    in any snapshot today (ET). A qualifying row whose
    OPPOSITE side already flagged earlier today (an earlier
    snapshot, never the same one) gets flip=true and flipSide
    naming that side, so the page can show a FLIPPED marker.
    Both sides can still flag — this only annotates.
  * One-side rule (props only, Nov 21 2026, George): in each
    snapshot, for each prop line (game, player, market, line),
    only the higher-bestEv side is a +EV play; the other side
    gets suppressed=true. The suppressed row still appears on
    the board with its EV intact — it is just not a +EV play,
    does not alert, and is not recorded in flaggedToday. Game
    rows are untouched. Tie on bestEv: prefer the side already
    in flaggedToday; otherwise the first row in snapshot order.
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


def _tier_interval(game_count):
    """Refresh interval (minutes) for today's game count."""
    if game_count >= 12:
        return 20
    if game_count >= 9:
        return 15
    if game_count >= 5:
        return 10
    return 5


def _last_snapshot(now_et):
    """(snapshotAt, distinct games today) from checked-out board.json.

    Returns (None, None) when there is no usable snapshot: file
    missing/unparseable, snapshotAt missing/unparseable, 0 rows,
    or no rows dated today. A 0-row snapshot is unknown, never
    "0 games" (PropLine returning zero games must not push the
    board onto the 5-minute tier).
    """
    try:
        with open(_board_path(), encoding="utf-8") as f:
            board = json.load(f)
    except (OSError, ValueError):
        return None, None
    rows = board.get("rows") or []
    if not rows:
        return None, None
    try:
        snap_at = datetime.fromisoformat(board["snapshotAt"])
    except (KeyError, ValueError, TypeError):
        return None, None
    if snap_at.tzinfo is None:
        snap_at = snap_at.replace(tzinfo=ET)
    today = now_et.strftime("%Y-%m-%d")
    games = {r.get("game") for r in rows if r.get("gameDate") == today}
    if not games:
        return None, None
    return snap_at, len(games)


def skip_if_not_due(now_et):
    """True when this tick should be skipped (zero PropLine calls).

    The tier interval comes from the distinct games on today's
    snapshot. First run of the day, or an unknown snapshot, is
    always due.
    """
    snap_at, game_count = _last_snapshot(now_et)
    if snap_at is None:
        return False
    interval = _tier_interval(game_count)
    age_min = (now_et - snap_at).total_seconds() / 60.0
    if age_min < interval:
        print(f"SKIPPED: last snapshot "
              f"{snap_at.strftime('%H:%M')} ET is {age_min:.1f} min old; "
              f"{game_count} games today -> {interval}-min tier — "
              f"no API calls, no refresh")
        return True
    return False


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


# Sharp books for the flip / alert qualification rule (same
# 9-book set as the site's SHARP_BOOKS and the watcher).
SHARP_BOOKS = frozenset({
    "pinnacle", "novig", "prophetx", "betonline", "bookmaker",
    "circa", "kalshi", "polymarket", "sporttrade",
})


def _line_str(line):
    """Line as it appears in a watcher lineKey: whole numbers
    without a decimal (3, not 3.0), .5 lines as-is, null -> ''."""
    if line is None or line == "":
        return ""
    f = float(line)
    if f == int(f):
        return str(int(f))
    return str(f)


def line_key(row):
    """Watcher lineKey for a slim row:
    {gameDate}|{game}|{player}|{market}|{side}|{line}."""
    return "|".join([
        str(row.get("gameDate") or ""),
        str(row.get("game") or ""),
        str(row.get("player") or ""),
        str(row.get("market") or ""),
        str(row.get("side") or ""),
        _line_str(row.get("line")),
    ])


def _is_sharp_anchored(row):
    """Same sharp-anchor rule as the alerts and the site's +EV
    plays filter: game rows are Pinnacle-anchored; prop rows
    need 2+ anchor books with at least 1 sharp book."""
    anchor = row.get("anchor") or []
    if row.get("kind") == "game":
        return anchor == ["Pinnacle (no-vig)"]
    if len(anchor) < 2:
        return False
    return any(str(b).lower() in SHARP_BOOKS for b in anchor)


def _qualifies(row, date_str):
    """A row counts as flagged today when it clears the alert
    threshold on a sharp anchor, dated today (ET)."""
    if row.get("gameDate") != date_str:
        return False
    ev = row.get("bestEv")
    if ev is None:
        return False
    try:
        ev = float(ev)
    except (TypeError, ValueError):
        return False
    return ev >= ALERT_THRESHOLD_EV and _is_sharp_anchored(row)


def _load_flagged_today(now_et):
    """lineKeys flagged in earlier snapshots today (ET).

    Read from the checked-out board.json's flaggedToday, but
    only when that snapshot is dated today; a missing file,
    corrupt JSON, a previous-day snapshot, or a non-list
    flaggedToday all mean []. A 0-row snapshot keeps its
    flaggedToday — an empty board is no data, not a reset.
    """
    try:
        with open(_board_path(), encoding="utf-8") as f:
            board = json.load(f)
    except (OSError, ValueError):
        return []
    try:
        snap_at = datetime.fromisoformat(board["snapshotAt"])
    except (KeyError, ValueError, TypeError):
        return []
    if snap_at.tzinfo is not None:
        snap_at = snap_at.astimezone(ET)
    else:
        snap_at = snap_at.replace(tzinfo=ET)
    if snap_at.strftime("%Y-%m-%d") != now_et.strftime("%Y-%m-%d"):
        return []
    flagged = board.get("flaggedToday") or []
    if not isinstance(flagged, list):
        return []
    return [k for k in flagged if isinstance(k, str)]


def _apply_flips(rows, old_flagged, date_str):
    """Mark flip rows and return the new flaggedToday list.

    A qualifying row is a flip when the OPPOSITE side of the
    same line already flagged earlier today — i.e. a key in
    the OLD flaggedToday list (never this snapshot's own keys)
    with the same date|game|player|market|line and a different
    side. flipSide names the side that flagged earlier. Both
    sides can still flag; this only annotates. Suppressed rows
    (one-side rule) are skipped — they neither flip nor enter
    flaggedToday. Returns the new flaggedToday: old union this
    snapshot's qualifying (and kept) keys.
    """
    by_line = {}
    for key in old_flagged:
        parts = key.split("|")
        if len(parts) != 6:
            continue
        by_line.setdefault(tuple(parts[:4] + [parts[5]]), set()).add(parts[4])
    new_keys = set()
    for row in rows:
        if not _qualifies(row, date_str):
            continue
        if row.get("suppressed"):
            continue
        key = line_key(row)
        new_keys.add(key)
        parts = key.split("|")
        others = (by_line.get(tuple(parts[:4] + [parts[5]]), set())
                  - {parts[4]})
        if others:
            row["flip"] = True
            row["flipSide"] = sorted(others)[0]
    return sorted(set(old_flagged) | new_keys)


def _apply_one_side(rows, old_flagged, date_str):
    """Per-snapshot one-side rule (props only, Nov 21 2026).

    Group qualifying prop rows by (game, player, market, line).
    When both Over and Under qualify in the same snapshot, keep
    only the higher-bestEv side and mark the other
    suppressed=True. The suppressed row still appears on the
    board with its EV intact — it is just not a +EV play, does
    not alert, and is not recorded in flaggedToday. Game rows
    are untouched. Tie on bestEv: prefer the side already in
    old flaggedToday (continuity); if neither is, keep the
    first row in snapshot order.
    """
    old_set = set(old_flagged)
    groups = {}
    order = []
    for i, row in enumerate(rows):
        if row.get("kind") != "prop":
            continue
        if not _qualifies(row, date_str):
            continue
        group_key = (row.get("gameDate"), row.get("game"),
                     row.get("player"), row.get("market"),
                     _line_str(row.get("line")))
        if group_key not in groups:
            groups[group_key] = []
            order.append(group_key)
        groups[group_key].append((i, row))
    for group_key in order:
        members = groups[group_key]
        if len(members) == 1:
            continue
        members.sort(key=lambda item: (
            -float(item[1].get("bestEv")),
            0 if line_key(item[1]) in old_set else 1,
            item[0]))
        for _i, row in members[1:]:
            row["suppressed"] = True


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

    slim_rows = [slim_row(r) for r in dash_rows]
    old_flagged = _load_flagged_today(now_et)
    # Order per run: (1) old flaggedToday loaded above;
    # (2) qualification is computed inside the helpers;
    # (3) one-side suppression within this snapshot;
    # (4) flips for the KEPT qualifying rows vs OLD flaggedToday;
    # (5) new flaggedToday = old union kept qualifying keys.
    _apply_one_side(slim_rows, old_flagged, date_str)
    new_flagged = _apply_flips(slim_rows, old_flagged, date_str)

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "snapshotLabel": label,
        "snapshotAt": now_et.isoformat(),
        "rowCount": len(dash_rows),
        "thresholdPct": int(ALERT_THRESHOLD_EV),
        "flaggedToday": new_flagged,
        "rows": slim_rows,
    }
    with open(_board_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))

    print(f"LIVE calls={counter[0]} "
          f"today_total={today_total(date_str, _log_path())} "
          f"events={len(events)} game_rows={len(game_rows)} "
          f"prop_rows={len(prop_rows)} board_rows={len(dash_rows)} "
          f"flips={sum(1 for r in slim_rows if r.get('flip'))} "
          f"suppressed={sum(1 for r in slim_rows if r.get('suppressed'))} "
          f"flagged_today={len(new_flagged)}")
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
    if skip_if_not_due(now_et):
        return 0
    status, _calls = build(now_et)
    return 0 if status in ("LIVE", "BUDGET_EXCEEDED") else 3


if __name__ == "__main__":
    raise SystemExit(main())
