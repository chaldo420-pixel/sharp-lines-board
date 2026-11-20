"""
sharp-lines-model-v1 — poll.py
One 15-minute refresh cycle for the Sharp Lines Dashboard.

Window (George, Nov 19 2026): run ONLY 08:00-23:00 ET.
Do NOT run 23:00-08:00 ET. Outside the window the poller exits
silently (status SKIPPED): no PropLine calls, no budget counted,
no dashboard touch, no alerts.

Cadence: every 15 minutes in the window = ~60 refreshes/day.
Budget: PropLine free tier 1,000 requests/day. One refresh costs
1 (events list) + N (one odds call per NBA game today), because
one odds call returns every book and every market for that game
("1 request = 1 debit", markets are a free filter). Measured
Nov 19 2026: 5 calls/refresh on a 4-game slate (1 + 4). A full
10-12 game slate is ~11-13 calls/refresh (~660-780/day at 15
min); a 15-game slate would be ~16/refresh, so the 850-call
stop below may cut the last few refreshes on the biggest
slates — by design, never by silently exceeding the cap.
FanDuel adds NO extra calls: it arrives in the same PropLine
response as DraftKings and Pinnacle.
The poller stops for the day once today's counted calls would
pass 850 (headroom below the 1,000 cap). The counter lives in
requests_log.jsonl (one line per refresh: date + calls) and is
checked BEFORE the odds calls, so a second run cannot blow it.

Alerts: only NEW +EV >= 2% lines create an alert, at EITHER
book (DraftKings or FanDuel). One alert per line: if both books
qualify, the single alert names both. seen_alerts.json
(date-scoped) dedups them, so a line that stays +EV across
refreshes does not spam. new_alerts.json is the handoff to the
cron run: it updates the dashboard (clearsnapshot, importcsv
game + props, refreshlines) and reports new alerts to George.

Live data: fetch_propline (custom.propline connector, query_param
apiKey via surrogate URL; env PROPLINE_API_KEY for local runs).
The key is never logged or written to a file. If no credential
is available, poll.py writes KEY_NEEDED.md and exits 3 without
making any API call.

Dry run: --dry-run runs the model on the sample CSVs
(lines.csv + props_main.csv) into /tmp, verifies row/alert
counts, and writes nothing to the live outputs, seen_alerts,
or the dashboard.

Usage:
    python3 poll.py              # live refresh (window + budget checked)
    python3 poll.py --dry-run    # sample-mode verification, no API calls
    python3 poll.py --now 07:30  # test hook: pretend it is HH:MM ET
No bets are placed anywhere in this file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_propline import fetch_all, list_events, parse_game_lines, parse_props, _odds_for_event  # noqa: E402
from model import compare_row  # noqa: E402
from props import compare_prop  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
WINDOW_START_HOUR = 8    # 08:00 ET inclusive
WINDOW_END_HOUR = 23     # 23:00 ET exclusive: do NOT run 23:00-08:00
BUDGET_CAP = 850         # stop for the day above this (free tier: 1000)
BUDGET_ESTIMATE_CALLS = 16  # 1 events list + up to 15 games, pre-list estimate
ALERT_THRESHOLD_EV = 2.0
MAIN_CHAT_ID = "4ef1ae0c-6927-42fb-94cd-58a145a41b13"

# Bet links — PropLine only, no other source (George, Nov 19 2026).
# Verified from live PropLine odds responses: PropLine returns NO
# per-selection link/URL and NO marketId/selectionId for any book
# (every book's link/app_link fields are null), so there is no
# exact tap-to-bet link from PropLine for either book. Do NOT
# construct a DraftKings ?outcomes= or FanDuel addToBetslip URL —
# the IDs those URLs need do not exist in the response.
# No bet links at all (George, Nov 19 2026: remove the FanDuel
# link — it does not connect to the bet). PropLine returns no
# per-selection link/URL and no marketId/selectionId for any book,
# so there is no exact tap-to-bet link for either book, and the
# FanDuel NBA page link is removed too — it only opened the
# book's NBA page, not the bet. Do NOT construct a DraftKings
# ?outcomes= or FanDuel addToBetslip URL — the IDs those URLs
# need do not exist in the response. Alerts carry only the book
# + game + exact line + EV.
BOOK_PAGE_LINKS = {"DraftKings": "",
                   "FanDuel": ""}
LINK_KIND = ""

GAME_FIELDS = ["game", "market", "side", "handicap", "pinnacle_handicap",
               "pinnacle_american", "pinnacle_fair_prob",
               "pinnacle_fair_decimal", "pinnacle_fair_american",
               "draftkings_american", "draftkings_decimal",
               "draftkings_implied_prob", "ev_pct", "clv_pct",
               "kelly_full_stake", "kelly_quarter_stake",
               "handicap_diff", "flag",
               "fanduel_american", "fanduel_decimal",
               "fanduel_implied_prob", "fanduel_ev_pct",
               "fanduel_handicap_diff", "fanduel_flag",
               "draftkings_link", "fanduel_link", "link_kind",
               "method", "timestamp", "source"]
PROP_FIELDS = ["game", "player", "market", "side", "line",
               "draftkings_american", "draftkings_decimal",
               "draftkings_implied_prob", "anchor", "anchor_books",
               "fair_prob", "fair_decimal", "fair_american",
               "ev_pct", "clv_pct", "kelly_full_stake",
               "kelly_quarter_stake", "flag",
               "fanduel_american", "fanduel_decimal",
               "fanduel_implied_prob", "fanduel_ev_pct", "fanduel_flag",
               "draftkings_link", "fanduel_link", "link_kind",
               "method", "timestamp", "source"]

# Dashboard CSV: the slimmed prop fields actually imported into
# the Sharp Lines Dashboard. Dropping clv_pct (always empty),
# method, anchor_books (duplicated inside anchor), kelly_full_stake
# and source brings a 4-game slate's props file from ~132KB to
# under the 100,000-char importcsv limit, so the whole board
# loads in ONE import call instead of 2–3 chunks (George,
# Nov 19 2026: the 10-minute load misses lines). The full
# props_comparison.csv keeps every column for audit; only the
# dashboard copy is slimmed. timestamp is KEPT: the dashboard
# takes each row's snapshotAt from it. On a big slate the slim
# file can still exceed 100K — the cron falls back to 2 chunks
# then (see the cron body).
DASHBOARD_PROP_FIELDS = [f for f in PROP_FIELDS
                         if f not in ("clv_pct", "method",
                                      "anchor_books",
                                      "kelly_full_stake", "source")]


# ------------------------------------------------------------------
# Window
# ------------------------------------------------------------------

def in_window(now_et):
    """True only 08:00 <= time < 23:00 ET."""
    return WINDOW_START_HOUR <= now_et.hour < WINDOW_END_HOUR


# ------------------------------------------------------------------
# Budget counter (requests_log.jsonl)
# ------------------------------------------------------------------

def _log_path():
    return os.path.join(BASE, "requests_log.jsonl")


def today_total(date_str, path=None):
    total = 0
    p = path or _log_path()
    if not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("date") == date_str:
                total += int(rec.get("calls", 0))
    return total


def log_calls(date_str, calls, mode, note=""):
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date_str, "ts": datetime.now(ET).isoformat(),
            "calls": int(calls), "mode": mode, "note": note,
        }) + "\n")


def budget_ok(date_str, estimated_calls, path=None):
    """True if today's total + estimated calls stays <= cap."""
    return today_total(date_str, path) + int(estimated_calls) <= BUDGET_CAP


# ------------------------------------------------------------------
# Seen alerts (dedup, date-scoped)
# ------------------------------------------------------------------

def _seen_path():
    return os.path.join(BASE, "seen_alerts.json")


def load_seen(date_str):
    p = _seen_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("date") != date_str:
        return {}
    return data.get("keys", {})


def save_seen(date_str, keys):
    with open(_seen_path(), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "keys": keys}, f, indent=2, sort_keys=True)


def alert_key(row):
    """Stable identity for one comparison row."""
    if row.get("player"):
        return "|".join(str(x) for x in (
            row.get("game"), "prop", row.get("player"), row.get("market"),
            row.get("side"), row.get("line")))
    return "|".join(str(x) for x in (
        row.get("game"), "game", row.get("market"), row.get("side"),
        row.get("handicap")))


def alert_label(row, books):
    """'DraftKings: ...', 'FanDuel: ...', or 'DraftKings + FanDuel: ...'."""
    if row.get("player"):
        core = (f"{row['player']} {row['market']} {row['side']} "
                f"{row['line']} — {row['game']}")
    else:
        core = f"{row['market']} {row['side']} — {row['game']}"
    return f"{' + '.join(books)}: {core}"


def _ev_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prop_alertable(row):
    """True only for a prop row with a SHARP anchor.

    Game rows (no "player") are Pinnacle-anchored and are not
    gated here. A prop row alerts only when its anchor has
    >= 2 books AND at least one sharp book (Circa, BookMaker,
    Kalshi, Polymarket, Novig). A soft-book-only consensus —
    e.g. BetMGM alone — is GemFinder in different clothes and
    never alerts, however high its EV looks (Nov 19 2026).
    """
    if not row.get("player"):
        return True
    books = [b.strip().lower()
             for b in str(row.get("anchor_books") or "").split(",")
             if b.strip()]
    if len(books) < 2:
        return False
    return any(s in b for b in books
               for s in ("circa", "bookmaker", "kalshi",
                         "polymarket", "novig"))


def new_alerts(rows, seen):
    """Rows with EV >= threshold at EITHER book, not already seen.

    One alert per line: if both DraftKings and FanDuel qualify,
    the single alert names both books. A line already alerted
    today does not re-alert. Prop rows must pass
    _prop_alertable (sharp anchor); game rows are not gated.
    """
    new = []
    for r in rows:
        qualifying = []
        dk_ev = _ev_or_none(r.get("ev_pct"))
        fd_ev = _ev_or_none(r.get("fanduel_ev_pct"))
        if dk_ev is not None and dk_ev >= ALERT_THRESHOLD_EV:
            qualifying.append(("DraftKings", dk_ev))
        if fd_ev is not None and fd_ev >= ALERT_THRESHOLD_EV:
            qualifying.append(("FanDuel", fd_ev))
        if not qualifying:
            continue
        if not _prop_alertable(r):
            continue
        key = alert_key(r)
        if key in seen:
            continue
        best = max(ev for _b, ev in qualifying)
        books = [b for b, _ev in qualifying]
        best_book = max(qualifying, key=lambda kv: kv[1])[0]
        seen[key] = {"ev_pct": best, "books": books,
                     "first_seen": datetime.now(ET).isoformat()}
        new.append({"key": key, "label": alert_label(r, books),
                    "ev_pct": best, "books": books,
                    "best_book": best_book,
                    "link": BOOK_PAGE_LINKS.get(best_book, ""),
                    "link_kind": LINK_KIND if BOOK_PAGE_LINKS.get(best_book) else "",
                    "draftkings_ev_pct": dk_ev, "fanduel_ev_pct": fd_ev,
                    "game": r.get("game"), "market": r.get("market"),
                    "side": r.get("side"),
                    "player": r.get("player"),
                    "line": r.get("line"),
                    "draftkings_american": r.get("draftkings_american"),
                    "fanduel_american": r.get("fanduel_american"),
                    "fair_prob": r.get("fair_prob")
                    or r.get("pinnacle_fair_prob")})
    return new, seen


# ------------------------------------------------------------------
# Model run on live rows
# ------------------------------------------------------------------

def run_game_rows(raw_rows, timestamp, source, bankroll=1000.0):
    out = []
    for r in raw_rows:
        row = compare_row(
            game=r["game"], market=r["market"], side=r["side"],
            pinnacle_a_american=float(r["pinnacle_american"]),
            pinnacle_b_american=float(r["pinnacle_other_american"]),
            draftkings_american=float(r["draftkings_american"]),
            pinnacle_handicap=r.get("pinnacle_handicap") or None,
            draftkings_handicap=r.get("draftkings_handicap") or None,
            fanduel_american=r.get("fanduel_american") or None,
            fanduel_handicap=r.get("fanduel_handicap") or None,
            bankroll=bankroll)
        row["clv_pct"] = ""
        row["timestamp"] = timestamp
        row["source"] = source
        row["draftkings_link"] = BOOK_PAGE_LINKS["DraftKings"]
        row["fanduel_link"] = BOOK_PAGE_LINKS["FanDuel"]
        row["link_kind"] = LINK_KIND if BOOK_PAGE_LINKS["FanDuel"] else ""
        out.append(row)
    return out


def run_prop_rows(raw_rows, timestamp, source, bankroll=1000.0):
    out = []
    # Anchor excludes BOTH checked books (DraftKings and FanDuel).
    # Every other book PropLine returns at the same line goes in
    # — sharp books (Circa, BookMaker, exchanges) are what make
    # the anchor sharp; soft books widen the consensus only.
    book_cols = (("Hard Rock Bet", "hr_over", "hr_under"),
                 ("BookMaker", "bm_over", "bm_under"),
                 ("BetMGM", "mgm_over", "mgm_under"),
                 ("Circa", "ca_over", "ca_under"),
                 ("BetOnline", "bo_over", "bo_under"),
                 ("Bovada", "bv_over", "bv_under"),
                 ("Kalshi", "ka_over", "ka_under"),
                 ("Polymarket", "pm_over", "pm_under"),
                 ("Novig", "nv_over", "nv_under"),
                 ("ProphetX", "px_over", "px_under"),
                 ("Sporttrade", "st_over", "st_under"),
                 ("BetRivers", "br_over", "br_under"),
                 ("Caesars", "cz_over", "cz_under"),
                 ("Fanatics", "fa_over", "fa_under"),
                 ("bet365", "b365_over", "b365_under"))
    for r in raw_rows:
        book_prices = {}
        for book, ok, uk in book_cols:
            if r.get(ok) not in (None, "") and r.get(uk) not in (None, ""):
                book_prices[book] = (float(r[ok]), float(r[uk]))
        for side, dk_key, fd_key in (("Over", "dk_over", "fd_over"),
                                     ("Under", "dk_under", "fd_under")):
            row = compare_prop(
                game=r["game"], player=r["player"], market=r["market"],
                side=side, line=float(r["line"]),
                draftkings_american=float(r[dk_key]),
                book_prices=book_prices if book_prices else None,
                fanduel_american=r.get(fd_key) or None,
                bankroll=bankroll)
            row["clv_pct"] = ""
            row["source"] = source
            row["timestamp"] = timestamp
            row["draftkings_link"] = BOOK_PAGE_LINKS["DraftKings"]
            row["fanduel_link"] = BOOK_PAGE_LINKS["FanDuel"]
            row["link_kind"] = LINK_KIND if BOOK_PAGE_LINKS["FanDuel"] else ""
            out.append(row)
    return out


# ------------------------------------------------------------------
# CSV writing
# ------------------------------------------------------------------

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _comma_free(value):
    # The dashboard's importcsv splits on commas inside quoted
    # fields, so the dashboard copy must contain no commas at all.
    if value is None:
        return ""
    return str(value).replace(",", " + ")


def write_dashboard_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: _comma_free(r.get(k, "")) for k in fields})


def _append_poll_log(line):
    with open(os.path.join(BASE, "poll.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------
# Live refresh
# ------------------------------------------------------------------

def live_refresh(now_et):
    date_str = now_et.strftime("%Y-%m-%d")
    ts = now_et.strftime("%Y-%m-%d %H:%M ET")
    source = f"PropLine live {ts}"
    counter = [0]

    # Budget pre-check (estimate) before any API call.
    if not budget_ok(date_str, BUDGET_ESTIMATE_CALLS):
        msg = (f"BUDGET_EXCEEDED: today {today_total(date_str)} calls + "
               f"~{BUDGET_ESTIMATE_CALLS} estimated > cap {BUDGET_CAP}; "
               f"no refresh, no API calls")
        print(msg)
        _append_poll_log(f"{ts} BUDGET_EXCEEDED today={today_total(date_str)}")
        return {"status": "BUDGET_EXCEEDED", "calls": 0, "new_alerts": []}

    try:
        events = list_events(counter)
    except Exception as exc:
        if "KEY-NEEDED" in str(exc) or "credential" in str(exc).lower():
            _write_key_needed()
            print("KEY-NEEDED: PropLine credential not available; "
                  "see KEY_NEEDED.md — no API calls made")
            return {"status": "KEY-NEEDED", "calls": counter[0],
                    "new_alerts": []}
        raise

    # Exact budget check now the event count is known.
    if not budget_ok(date_str, len(events)):
        msg = (f"BUDGET_EXCEEDED: today {today_total(date_str)} calls + "
               f"{len(events)} event calls > cap {BUDGET_CAP}; "
               f"stopped before odds calls")
        print(msg)
        log_calls(date_str, counter[0], "live",
                  note="events list only, stopped on budget")
        _append_poll_log(f"{ts} BUDGET_EXCEEDED events={len(events)}")
        return {"status": "BUDGET_EXCEEDED", "calls": counter[0],
                "new_alerts": []}

    raw_game, raw_props = [], []
    for ev in events:
        game_label = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        payload = _odds_for_event(ev.get("id"), counter)
        raw_game.extend(parse_game_lines(payload, game_label))
        raw_props.extend(parse_props(payload, game_label))

    log_calls(date_str, counter[0], "live",
              note=f"{len(events)} events, {len(raw_game)} game rows, "
                   f"{len(raw_props)} prop groups")
    _remove_key_needed()

    # Persist the live snapshot inputs (auditable, re-runnable).
    write_csv(os.path.join(BASE, "lines_live.csv"), raw_game,
              ["game", "market", "side", "pinnacle_american",
               "pinnacle_other_american", "draftkings_american",
               "pinnacle_handicap", "draftkings_handicap",
               "fanduel_american", "fanduel_handicap"])
    write_csv(os.path.join(BASE, "props_main_live.csv"), raw_props,
              ["game", "player", "market", "line", "dk_over", "dk_under",
               "hr_over", "hr_under", "fd_over", "fd_under",
               "bm_over", "bm_under", "mgm_over", "mgm_under",
               "ca_over", "ca_under", "bo_over", "bo_under",
               "bv_over", "bv_under", "ka_over", "ka_under",
               "pm_over", "pm_under", "nv_over", "nv_under",
               "px_over", "px_under", "st_over", "st_under",
               "br_over", "br_under", "cz_over", "cz_under",
               "fa_over", "fa_under", "b365_over", "b365_under"])

    game_rows = run_game_rows(raw_game, ts, source)
    prop_rows = run_prop_rows(raw_props, ts, source)
    write_csv(os.path.join(BASE, "comparison.csv"), game_rows, GAME_FIELDS)
    write_csv(os.path.join(BASE, "props_comparison.csv"), prop_rows, PROP_FIELDS)
    write_dashboard_csv(
        os.path.join(BASE, "props_comparison_dashboard.csv"),
        prop_rows, DASHBOARD_PROP_FIELDS)

    seen = load_seen(date_str)
    new, seen = new_alerts(game_rows + prop_rows, seen)
    save_seen(date_str, seen)
    with open(os.path.join(BASE, "new_alerts.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": date_str, "ts": ts, "threshold_ev": ALERT_THRESHOLD_EV,
                   "alerts": new}, f, indent=2)

    _append_poll_log(
        f"{ts} LIVE calls={counter[0]} today={today_total(date_str)} "
        f"events={len(events)} game_rows={len(game_rows)} "
        f"prop_rows={len(prop_rows)} new_alerts={len(new)}")
    print(f"LIVE calls={counter[0]} today_total={today_total(date_str)} "
          f"events={len(events)} game_rows={len(game_rows)} "
          f"prop_rows={len(prop_rows)} new_alerts={len(new)}")
    for a in new:
        dk = a.get("draftkings_ev_pct")
        fd = a.get("fanduel_ev_pct")
        dk_s = f"{dk}%" if dk is not None else "—"
        fd_s = f"{fd}%" if fd is not None else "—"
        print(f"  NEW ALERT +{a['ev_pct']}% EV: {a['label']} "
              f"(DK EV {dk_s}, FD EV {fd_s})")
    return {"status": "LIVE", "calls": counter[0], "events": len(events),
            "game_rows": len(game_rows), "prop_rows": len(prop_rows),
            "new_alerts": new}


def _write_key_needed():
    with open(os.path.join(BASE, "KEY_NEEDED.md"), "w",
              encoding="utf-8") as f:
        f.write(
            "# KEY-NEEDED — PropLine credential missing\n\n"
            "The 15-minute Sharp Lines poller could not run live because\n"
            "no PropLine credential was available.\n\n"
            "Fix: connect PropLine (custom.propline, free tier, 1,000\n"
            "requests/day, no card) at prop-line.com, or set\n"
            "PROPLINE_API_KEY in the environment for local runs.\n"
            "Never paste the key in chat. The poller makes no API\n"
            "calls and sends no alerts until the credential exists.\n")


def _remove_key_needed():
    p = os.path.join(BASE, "KEY_NEEDED.md")
    if os.path.exists(p):
        os.remove(p)


# ------------------------------------------------------------------
# Dry run (sample mode, no API calls, nothing live touched)
# ------------------------------------------------------------------

def dry_run():
    import run_tonight  # noqa: PLC0415
    game_rows = run_tonight.run_game_lines(1000.0, "multiplicative")
    prop_rows = run_tonight.run_props(1000.0, "multiplicative")
    outdir = "/tmp/poll_dryrun"
    os.makedirs(outdir, exist_ok=True)
    write_csv(os.path.join(outdir, "comparison.csv"), game_rows, GAME_FIELDS)
    write_csv(os.path.join(outdir, "props_comparison.csv"), prop_rows, PROP_FIELDS)
    write_dashboard_csv(
        os.path.join(outdir, "props_comparison_dashboard.csv"),
        prop_rows, DASHBOARD_PROP_FIELDS)
    game_ev = [r for r in game_rows
               if _ev_or_none(r.get("ev_pct")) is not None
               and _ev_or_none(r.get("ev_pct")) >= ALERT_THRESHOLD_EV
               or _ev_or_none(r.get("fanduel_ev_pct")) is not None
               and _ev_or_none(r.get("fanduel_ev_pct")) >= ALERT_THRESHOLD_EV]
    prop_ev = [r for r in prop_rows
               if _ev_or_none(r.get("ev_pct")) is not None
               and _ev_or_none(r.get("ev_pct")) >= ALERT_THRESHOLD_EV
               or _ev_or_none(r.get("fanduel_ev_pct")) is not None
               and _ev_or_none(r.get("fanduel_ev_pct")) >= ALERT_THRESHOLD_EV]
    print(f"DRY-RUN game_rows={len(game_rows)} prop_rows={len(prop_rows)} "
          f"alerts>=2% game={len(game_ev)} props={len(prop_ev)}")
    print(f"DRY-RUN outputs in {outdir} (live files untouched, "
          f"no API calls, no seen_alerts change)")
    return {"status": "DRY-RUN", "calls": 0,
            "game_rows": len(game_rows), "prop_rows": len(prop_rows),
            "new_alerts": game_ev + prop_ev}


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="sample mode: no API calls, nothing live touched")
    ap.add_argument("--now", default=None,
                    help="test hook: pretend it is HH:MM ET, e.g. 07:30")
    args = ap.parse_args(argv)

    if args.dry_run:
        dry_run()
        return 0

    if args.now:
        hh, mm = args.now.split(":")
        now_et = datetime.now(ET).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0)
    else:
        now_et = datetime.now(ET)

    if not in_window(now_et):
        msg = (f"SKIPPED: {now_et.strftime('%H:%M')} ET is outside the "
               f"08:00-23:00 ET window — no API calls, no refresh")
        print(msg)
        _append_poll_log(
            f"{now_et.strftime('%Y-%m-%d %H:%M ET')} SKIPPED outside window")
        return 0

    result = live_refresh(now_et)
    return 0 if result["status"] in ("LIVE", "BUDGET_EXCEEDED") else 3


if __name__ == "__main__":
    raise SystemExit(main())
