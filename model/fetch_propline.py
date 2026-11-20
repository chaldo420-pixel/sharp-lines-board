"""
sharp-lines-model-v1 — fetch_propline.py
Live PropLine fetcher for the 20-minute Sharp Lines poller.

API (verified against PropLine docs, Nov 19 2026):
  * Base: https://api.prop-line.com/v1
  * List events:  GET /sports/basketball_nba/events?apiKey=KEY
  * Per-event odds: GET /sports/basketball_nba/events/{id}/odds
        ?apiKey=KEY&markets=h2h,spreads,totals,player_points,
          player_rebounds,player_assists,player_threes
        &bookmakers=draftkings,pinnacle,bookmaker,fanduel,betmgm,hard_rock_bet
  * Billing: "1 request = 1 debit" per odds call; markets are a free
    filter, not a credit multiplier. So one refresh costs
    1 (events list) + N (one odds call per NBA game today, ~12).
    Free tier: 1,000 requests/day, no card.
  * Response shape is the-odds-api compatible:
    bookmakers[].markets[].outcomes[] with outcome "price" in
    AMERICAN odds (e.g. 240 / -333), "point" the handicap/line,
    "name" the team or Over/Under, "description" the player name
    on prop markets, and market "line_type" ("main" vs alt).
  * Bookmaker "key" values are normalised here (e.g. "bookmaker",
    "hard_rock_bet"); if the API rejects the bookmakers filter
    with a 400, we retry once WITHOUT the filter and filter
    client-side, so one wrong key spelling cannot kill a refresh.

Rules enforced here:
  * Key comes ONLY from the registered custom connector
    (custom.propline, query_param apiKey via the surrogate URL
    helper) or, for local runs, the environment
    (PROPLINE_API_KEY). Never hardcoded, never logged, never
    written to a file.
    The key is never included in any log line (URLs are logged
    as endpoint paths only, never with query strings).
  * MAIN lines only. Prop markets are kept only when
    market["line_type"] == "main". Alt lines are dropped.
  * No bets are placed. This module only reads odds.
  * Every HTTP call is counted (see budget.py helpers in poll.py
    via the shared counter in this module: count_calls()).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://api.prop-line.com/v1"
SPORT = "basketball_nba"
ET = ZoneInfo("America/New_York")

GAME_MARKETS = ("h2h", "spreads", "totals")
PROP_MARKETS = ("player_points", "player_rebounds",
                "player_assists", "player_threes")
ALL_MARKETS = GAME_MARKETS + PROP_MARKETS

# Bookmaker keys to request (where available). Titles are taken
# from the API response when present; these are fallbacks.
# SHARP ANCHOR (Nov 20 2026, fitted weights): the prop anchor
# must contain a sharp source — Pinnacle, Novig, ProphetX,
# BetOnline (weight 3) or BookMaker, Circa, Kalshi, Polymarket,
# Sporttrade (weight 2) — not just soft books. The extra
# keys cost NO extra PropLine calls: they are a filter on the
# same per-event odds response. Soft books are still fetched
# (they widen the consensus), but a prop alert requires a
# sharp book in the anchor (see props.py / poll.py).
BOOKMAKERS = ("draftkings", "pinnacle", "bookmaker",
              "fanduel", "betmgm", "hard_rock_bet",
              "circa", "circa_sports",
              "betonline", "betonline_ag", "bovada",
              "kalshi", "polymarket", "novig", "prophetx",
              "sporttrade", "betrivers", "caesars",
              "fanatics", "bet365")

BOOK_DISPLAY = {
    "draftkings": "DraftKings",
    "pinnacle": "Pinnacle",
    "bookmaker": "BookMaker",
    "bookmaker_eu": "BookMaker",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "hard_rock_bet": "Hard Rock Bet",
    "hardrockbet": "Hard Rock Bet",
    "hard_rock": "Hard Rock Bet",
    "circa": "Circa",
    "circa_sports": "Circa",
    "betonline": "BetOnline",
    "betonline_ag": "BetOnline",
    "bovada": "Bovada",
    "kalshi": "Kalshi",
    "polymarket": "Polymarket",
    "novig": "Novig",
    "prophetx": "ProphetX",
    "sporttrade": "Sporttrade",
    "betrivers": "BetRivers",
    "caesars": "Caesars",
    "fanatics": "Fanatics",
    "bet365": "bet365",
}

PROP_MARKET_DISPLAY = {
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "Threes",
}


def get_key():
    """Return the PropLine key from the environment, or None.

    The value itself is never printed or logged. The preferred
    path in scheduled runs is the registered custom connector
    (custom.propline), used by _get_json when no env key is set.
    """
    return os.environ.get("PROPLINE_API_KEY") or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _build_url(path, params):
    """Build the request URL with the credential attached.

    Env key (local runs) goes in as apiKey=<key>. Otherwise the
    registered custom.propline credential is attached via
    url_with_surrogate_query_param (query_param placement):
    the surrogate is already in the URL, so do NOT also call
    add_surrogate_to_request (it errors for query_param
    credentials). The returned URL is never logged.
    """
    query = dict(params or {})
    base = BASE_URL + path
    if query:
        base += "?" + urllib.parse.urlencode(query)
    key = get_key()
    if key:
        sep = "&" if "?" in base else "?"
        return base + sep + "apiKey=" + urllib.parse.quote(key)
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import url_with_surrogate_query_param
    return url_with_surrogate_query_param(
        base, "custom.propline", allowed_hosts=["api.prop-line.com"])


def _get_json(path, params, counter):
    """GET one PropLine endpoint. Returns parsed JSON.

    `counter` is a mutable [int] counting HTTP calls made.
    Raises RuntimeError on HTTP errors (with status, no key).
    The request URL (which carries the credential) is never
    logged; only the endpoint path is.
    """
    url = _build_url(path, params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    counter[0] += 1
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not include the URL (it carries the credential).
        raise RuntimeError(
            f"PropLine request failed: {path} -> HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PropLine request failed: {path} -> {exc}") from exc


def list_events(counter):
    """Return today's NBA events (list of dicts).

    Keeps events whose commence_time falls on today's date in
    America/New_York and which are not flagged live.
    """
    data = _get_json(f"/sports/{SPORT}/events", {}, counter)
    events = data if isinstance(data, list) else data.get("events", [])
    today = datetime.now(ET).date()
    kept = []
    for ev in events:
        ct = ev.get("commence_time")
        if not ct:
            continue
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(ET).date() != today:
            continue
        if ev.get("live"):
            continue
        kept.append(ev)
    return kept


def _odds_for_event(event_id, counter, use_filter=True):
    params = {"markets": ",".join(ALL_MARKETS)}
    if use_filter:
        params["bookmakers"] = ",".join(BOOKMAKERS)
    try:
        return _get_json(
            f"/sports/{SPORT}/events/{event_id}/odds", params, counter)
    except RuntimeError as exc:
        if use_filter and "HTTP 400" in str(exc):
            # One bad bookmaker key spelling must not kill the
            # refresh: retry unfiltered and filter client-side.
            # (The failed call already counted; the retry counts
            # as one more call, as it should.)
            return _get_json(
                f"/sports/{SPORT}/events/{event_id}/odds",
                {"markets": ",".join(ALL_MARKETS)}, counter)
        raise


def _book_title(bk):
    return bk.get("title") or BOOK_DISPLAY.get(
        str(bk.get("key", "")).lower(), str(bk.get("key", "")))


def _norm_book_key(bk):
    k = str(bk.get("key", "")).lower()
    if k in ("bookmaker_eu",):
        return "bookmaker"
    if k in ("hardrockbet", "hard_rock"):
        return "hard_rock_bet"
    if k in ("circa_sports",):
        return "circa"
    if k in ("betonline_ag",):
        return "betonline"
    return k


def _outcomes_by_book(payload):
    """Yield (book_key, book_title, market) for a payload.

    Payload is one event's odds response (dict with "bookmakers").
    Only requested bookmakers are yielded.
    """
    bookmakers = payload.get("bookmakers", []) if isinstance(payload, dict) else []
    for bk in bookmakers:
        key = _norm_book_key(bk)
        if key not in BOOKMAKERS:
            continue
        for mkt in bk.get("markets", []):
            yield key, _book_title(bk), mkt


def _american(price):
    """PropLine prices are American odds (240 / -333)."""
    return float(price)


def parse_game_lines(payload, game_label):
    """Parse h2h/spreads/totals into lines.csv-style rows.

    Returns a list of dicts with keys: game, market, side,
    pinnacle_american, pinnacle_other_american,
    draftkings_american, pinnacle_handicap, draftkings_handicap.
    One row per side per market. Rows are only built where BOTH
    Pinnacle (both sides, for the no-vig anchor) and DraftKings
    price the same side; a side missing either book is skipped,
    never invented.
    """
    # book -> market_key -> {(side_or_name, point): (name, price)}
    # Join on "side" (home/away), NOT on team name: DraftKings
    # uses short names ("MIL Bucks") while Pinnacle uses full
    # names ("Milwaukee Bucks"), so a name join matches nothing.
    # Totals have side None, so they fall back to the name
    # ("Over"/"Under"), which agrees across books.
    by_book = {}
    for key, _title, mkt in _outcomes_by_book(payload):
        mk = mkt.get("key")
        if mk not in GAME_MARKETS:
            continue
        # MAIN lines only: PropLine returns every alternate spread/
        # total and every team total in the same markets. An alt
        # total (e.g. Over 119.5) or a team total is NOT the game
        # total — treating it as one produced absurd EVs (+171%)
        # in the first live run. Game totals have team == None.
        if mkt.get("line_type") not in (None, "main"):
            continue
        if mk == "totals" and mkt.get("team") is not None:
            continue
        d = by_book.setdefault(key, {}).setdefault(mk, {})
        for oc in mkt.get("outcomes", []):
            join = oc.get("side") or oc.get("name")
            d[(join, oc.get("point"))] = (oc.get("name"),
                                          _american(oc["price"]))

    rows = []
    pin = by_book.get("pinnacle", {})
    dk = by_book.get("draftkings", {})
    fd = by_book.get("fanduel", {})
    if not pin or not dk:
        return rows

    # by_book is keyed (join, point); rebuild a per-side view.
    def _side_entries(book_dict, market):
        out = {}
        for (join, pt), (name, price) in book_dict.get(market, {}).items():
            out.setdefault(join, []).append((pt, name, price))
        return out

    dk_side = {m: _side_entries(dk, m) for m in GAME_MARKETS}
    fd_side = {m: _side_entries(fd, m) for m in GAME_MARKETS}

    def _pick(side_map, market, join, point):
        """(price, point) for a side: exact point first, else the
        book's main line for that side (EV must not be computed
        on it — compare_row flags line-differs)."""
        cands = side_map.get(market, {}).get(join, [])
        if not cands:
            return None, None
        for pt, _name, price in cands:
            if pt == point:
                return price, pt
        pt, _name, price = cands[0]
        return price, pt

    # --- moneyline -------------------------------------------------
    for (join, _pt), (name, pin_price) in pin.get("h2h", {}).items():
        others = [(j, v) for (j, _p), v in pin["h2h"].items()
                  if j != join]
        if len(others) != 1:
            continue
        other_price = others[0][1][1]
        dk_price, dk_pt = _pick(dk_side, "h2h", join, None)
        if dk_price is None:
            continue
        fd_price, fd_pt = _pick(fd_side, "h2h", join, None)
        rows.append({
            "game": game_label, "market": "moneyline",
            "side": f"{name} ML",
            "pinnacle_american": pin_price,
            "pinnacle_other_american": other_price,
            "draftkings_american": dk_price,
            "pinnacle_handicap": "", "draftkings_handicap": "",
            "fanduel_american": "" if fd_price is None else fd_price,
            "fanduel_handicap": "" if fd_pt is None else fd_pt,
        })

    # --- spreads ---------------------------------------------------
    for (join, pt), (name, pin_price) in pin.get("spreads", {}).items():
        if pt is None:
            continue
        others = [(j, v) for (j, _p), v in pin["spreads"].items()
                  if j != join]
        if len(others) != 1:
            continue
        other_price = others[0][1][1]
        dk_price, dk_pt = _pick(dk_side, "spreads", join, pt)
        if dk_price is None:
            continue
        fd_price, fd_pt = _pick(fd_side, "spreads", join, pt)
        rows.append({
            "game": game_label, "market": "spread",
            "side": f"{name} {float(pt):+.1f}",
            "pinnacle_american": pin_price,
            "pinnacle_other_american": other_price,
            "draftkings_american": dk_price,
            "pinnacle_handicap": pt, "draftkings_handicap": dk_pt,
            "fanduel_american": "" if fd_price is None else fd_price,
            "fanduel_handicap": "" if fd_pt is None else fd_pt,
        })

    # --- totals ----------------------------------------------------
    for (join, pt), (name, pin_price) in pin.get("totals", {}).items():
        if pt is None or name not in ("Over", "Under"):
            continue
        other_name = "Under" if name == "Over" else "Over"
        other_entry = pin["totals"].get((other_name, pt))
        if other_entry is None:
            continue
        dk_price, dk_pt = _pick(dk_side, "totals", join, pt)
        if dk_price is None:
            continue
        fd_price, fd_pt = _pick(fd_side, "totals", join, pt)
        rows.append({
            "game": game_label, "market": "total",
            "side": f"{name} {float(pt)}",
            "pinnacle_american": pin_price,
            "pinnacle_other_american": other_entry[1],
            "draftkings_american": dk_price,
            "pinnacle_handicap": pt, "draftkings_handicap": dk_pt,
            "fanduel_american": "" if fd_price is None else fd_price,
            "fanduel_handicap": "" if fd_pt is None else fd_pt,
        })
    return rows


def parse_props(payload, game_label):
    """Parse MAIN player props into props_main.csv-style rows.

    Returns a list of dicts with keys: game, player, market,
    line, dk_over, dk_under, hr_over, hr_under, fd_over,
    fd_under, bm_over, bm_under, mgm_over, mgm_under, plus
    one over/under pair per additional book (Pinnacle, Circa,
    BetOnline, Bovada, Kalshi, Polymarket, Novig, ProphetX,
    Sporttrade, BetRivers, Caesars, Fanatics, bet365).
    Only markets with line_type == "main" are kept (alt lines
    dropped). The DraftKings line governs: other books are only
    used at the SAME line as DraftKings for that player/market.
    A prop with no DraftKings price is skipped.
    """
    # (player, market, line) -> book_key -> (over, under)
    grouped = {}
    for key, _title, mkt in _outcomes_by_book(payload):
        mk = mkt.get("key")
        if mk not in PROP_MARKETS:
            continue
        if mkt.get("line_type") not in (None, "main"):
            continue  # alt lines are never main-board props
        for oc in mkt.get("outcomes", []):
            player = oc.get("description") or oc.get("name")
            side = oc.get("name")
            # PropLine uses name=Over/Under, description=player;
            # accept the swapped convention too.
            if side not in ("Over", "Under"):
                if oc.get("description") in ("Over", "Under"):
                    side = oc.get("description")
                    player = oc.get("name")
                else:
                    continue
            line = oc.get("point")
            if player is None or line is None:
                continue
            slot = grouped.setdefault(
                (player, PROP_MARKET_DISPLAY[mk], float(line)), {})
            over_under = slot.setdefault(key, [None, None])
            over_under[0 if side == "Over" else 1] = _american(oc["price"])

    rows = []
    col = {"pinnacle": ("pn_over", "pn_under"),
           "draftkings": ("dk_over", "dk_under"),
           "hard_rock_bet": ("hr_over", "hr_under"),
           "fanduel": ("fd_over", "fd_under"),
           "bookmaker": ("bm_over", "bm_under"),
           "betmgm": ("mgm_over", "mgm_under"),
           "circa": ("ca_over", "ca_under"),
           "betonline": ("bo_over", "bo_under"),
           "bovada": ("bv_over", "bv_under"),
           "kalshi": ("ka_over", "ka_under"),
           "polymarket": ("pm_over", "pm_under"),
           "novig": ("nv_over", "nv_under"),
           "prophetx": ("px_over", "px_under"),
           "sporttrade": ("st_over", "st_under"),
           "betrivers": ("br_over", "br_under"),
           "caesars": ("cz_over", "cz_under"),
           "fanatics": ("fa_over", "fa_under"),
           "bet365": ("b365_over", "b365_under")}
    for (player, market, line), books in sorted(grouped.items()):
        dk = books.get("draftkings")
        if not dk or dk[0] is None or dk[1] is None:
            continue
        row = {"game": game_label, "player": player, "market": market,
               "line": line}
        for bk, (ok, uk) in col.items():
            prices = books.get(bk, [None, None])
            row[ok] = "" if prices[0] is None else prices[0]
            row[uk] = "" if prices[1] is None else prices[1]
        rows.append(row)
    return rows


def fetch_all(counter):
    """One full refresh. Returns (game_rows, prop_rows, n_events).

    `counter` is a mutable [int] counting HTTP calls. Raises
    RuntimeError (KEY-NEEDED etc.) before any call if no key.
    """
    events = list_events(counter)
    game_rows, prop_rows = [], []
    for ev in events:
        game_label = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        payload = _odds_for_event(ev.get("id"), counter)
        game_rows.extend(parse_game_lines(payload, game_label))
        prop_rows.extend(parse_props(payload, game_label))
    return game_rows, prop_rows, len(events)
