"""
sharp-lines-model-v1 — props.py
Player props (Over/Under), NBA.

IMPORTANT LIMITATION — read this first
--------------------------------------
Pinnacle does NOT offer NBA player props. Its guest API exposes
no NBA props, and PropLine's docs note Pinnacle NBA coverage is
game lines only. So Pinnacle CANNOT be the sharp anchor for
player props the same way it is for moneyline / spread / total.

What v1.2 does instead for props
------------------------------
Anchor = consensus no-vig fair price across the OTHER books
that price the same prop at the same line/threshold:
  * collect every non-DraftKings book's Over and Under American
    prices for that player/market/line,
  * strip the vig from each book's two-way price (multiplicative
    or power, same as model.py),
  * take the MEDIAN of those books' fair Over probabilities
    (median, not mean, so one wild book cannot drag the anchor),
  * SHARP books get double weight in the median: BookMaker and
    Circa (the two sharp books that post NBA props), and the
    near-zero-vig exchanges Kalshi, Polymarket and Novig.
  * DraftKings AND FanDuel are NEVER in the anchor — both are
    books being checked in v1.1, and including either would be
    circular. (FanDuel was in the anchor in v1.0, before it
    became a checked book; it is excluded from Nov 19 2026.)

SHARP ANCHOR RULE (Nov 19 2026, George's call)
-----------------------------------------------
A soft-book-only consensus is NOT a sharp anchor — it is
GemFinder in different clothes (see the Nov 19 evening
discussion: the 19:07 board's 266 BetMGM-only rows). So:
  * an anchor with NO sharp book in it (Circa, BookMaker,
    Kalshi, Polymarket, Novig) is labelled soft / single-book
    and NEVER produces a +EV alert, however high its EV looks.
  * a prop alert requires >= 2 anchor books AND >= 1 sharp
    book in the anchor (enforced in poll.py new_alerts).
  * game lines are unaffected: their anchor is Pinnacle.

Flags:
  * >= 2 anchor books incl. a sharp book -> "sharp anchor"
    (n >= 3) or "thin sharp anchor" (n == 2)
  * >= 2 anchor books, no sharp book -> "soft consensus — do not alert"
  * 1 anchor book -> "single-book anchor — do not alert"
  * 0 anchor books -> "no sharp anchor — consensus only",
    EV is None. Do not report an EV for a DK-only prop.
  * If another book prices the same prop at a different line
    (e.g. DK Over 13.5, another book Over 12.5), that book is
    NOT used for that line. A 0.5 difference in the line is a
    different bet — same rule as spreads/totals in model.py.

Same math as game lines once the anchor is set: EV% vs the
consensus fair prob, Kelly (full and 1/4, capped), CLV% vs the
consensus close. Markets: points, rebounds, assists, threes,
steals, blocks, and combos Pts+Reb, Pts+Ast, Reb+Ast,
Pts+Reb+Ast. Each prop is an Over or an Under at one line.

Fallback used in the original Nov 19 sample run — RETIRED
-----------------------------------------------------------
The first Nov 19 run had no multi-book prop board in-session,
so its 6 sample props used a single-source model hit rate as
the fair probability, labelled anchor="sample-hit
(illustrative)". Those were ALT lines, not main lines, and
that fallback is now retired and its data removed: the live
props board runs on PropLine only (consensus anchor below),
and no alt lines are kept in this repo. Do not treat the old
sample EVs as consensus EVs, and do not mix alt lines into
the main board.
"""

from __future__ import annotations

from statistics import median

from model import (american_to_decimal, decimal_to_american,
                   ev_pct, fair_price_from_prob, implied_prob,
                   kelly_stake, no_vig)


# ------------------------------------------------------------------
# Consensus anchor
# ------------------------------------------------------------------

# Sharp books for props: the two sharp books that post NBA
# props (Circa, BookMaker) plus the near-zero-vig exchanges.
# A prop anchor with none of these in it is a soft consensus
# only — it never produces a +EV alert (see module docstring).
SHARP_BOOKS = ("bookmaker", "circa", "kalshi", "polymarket", "novig")

# Soft books that are FALLBACK-ONLY in the anchor (Nov 20 2026,
# George: "do what you think is best"). They are excluded
# whenever 2+ sharp books price the line, and used only to reach
# the 2-book minimum when sharp coverage is thin. Hard Rock Bet
# is not in this list — it stays at full weight.
SOFT_FALLBACK_BOOKS = ("betmgm", "betrivers", "fanatics")

# Canonical anchor order (same order as poll.py's book_cols and
# the dashboard's ANCHOR_BOOK_KEYS): anchor_books are listed
# in this order, and fallback books fill in this order.
_ANCHOR_ORDER = {name: i for i, name in enumerate((
    "hard rock bet", "bookmaker", "betmgm", "circa", "betonline",
    "bovada", "kalshi", "polymarket", "novig", "prophetx",
    "sporttrade", "betrivers", "caesars", "fanatics", "bet365"))}


def consensus_fair_prob(book_prices, method="multiplicative",
                        sharp_books=SHARP_BOOKS,
                        excluded_books=("draftkings", "dk",
                                        "fanduel", "fd")):
    """Median no-vig fair Over probability across non-checked books.

    book_prices: dict book_name -> (over_american, under_american)
    for the SAME player/market/line. Book names are matched
    case-insensitively; sharp books (BookMaker, Circa, Kalshi,
    Polymarket, Novig) get double weight in the median. Books in
    `excluded_books` (DraftKings and FanDuel — both are checked
    books) are never in the anchor, because that would be
    circular.

    Returns (fair_over_prob, n_books, anchor_books, flag).
    fair_over_prob is None when n_books == 0. Flags:
    "sharp anchor" (n >= 3, sharp book present), "thin sharp
    anchor" (n == 2, sharp book present), "soft consensus —
    do not alert" (n >= 2, no sharp book), "single-book
    anchor — do not alert" (n == 1).

    Fallback-only soft books (BetMGM, BetRivers, Fanatics):
    the anchor is built from the non-fallback candidates
    ("core") first; soft books are added ONLY as filler, in
    canonical anchor order, until the anchor reaches the
    2-book minimum — 0 if core already has >= 2 books, 1 if
    core has 1, 2 if core is empty. They therefore never sit
    in an anchor alongside 2+ sharp books, and never pad an
    anchor that already has 2 books. Anchor books are listed
    in canonical anchor order.
    """
    # Candidates: every non-excluded book with both Over and
    # Under prices at this line, with its no-vig fair Over
    # probability. Order is the canonical anchor order.
    candidates = []
    for book, (over_am, under_am) in book_prices.items():
        if book is None:
            continue
        name = str(book).strip()
        if name.lower() in excluded_books:
            continue  # never in the anchor — circular
        try:
            qo = implied_prob(over_am)
            qu = implied_prob(under_am)
            fo, _fu = no_vig([qo, qu], method=method)
        except (ValueError, ZeroDivisionError):
            continue
        candidates.append((name, fo))
    candidates.sort(key=lambda c: _ANCHOR_ORDER.get(c[0].lower(), 99))

    core = [(n, f) for n, f in candidates
            if n.lower() not in SOFT_FALLBACK_BOOKS]
    fallbacks = [(n, f) for n, f in candidates
                 if n.lower() in SOFT_FALLBACK_BOOKS]
    used = list(core)
    if len(used) < 2:
        # Fallback-only soft books fill the anchor up to the
        # 2-book minimum, in canonical anchor order.
        used.extend(fallbacks[:2 - len(used)])

    fair_overs = []
    anchor_books = []
    for name, fo in used:
        weight = 2 if any(s in name.lower() for s in sharp_books) else 1
        fair_overs.extend([fo] * weight)
        anchor_books.append(name)

    n = len(anchor_books)
    if n == 0:
        return None, 0, [], "no sharp anchor — consensus only"
    fair = median(fair_overs)
    has_sharp = any(
        any(s in str(b).strip().lower() for s in sharp_books)
        for b in anchor_books)
    if n == 1:
        # One book is one book, even if it is sharp — a single
        # source can never be a sharp anchor on its own.
        flag = "single-book anchor — do not alert"
    elif has_sharp:
        flag = "sharp anchor" if n >= 3 else "thin sharp anchor"
    else:
        flag = "soft consensus — do not alert"
    return fair, n, anchor_books, flag


# ------------------------------------------------------------------
# One prop comparison
# ------------------------------------------------------------------

def compare_prop(game: str, player: str, market: str, side: str,
                 line: float, draftkings_american: float,
                 book_prices=None, fair_prob_override=None,
                 anchor_label: str = "consensus",
                 fanduel_american=None,
                 method: str = "multiplicative",
                 bankroll: float = 1000.0):
    """Compare one DK prop price against an anchor fair probability.

    side: "Over" or "Under". line: the prop threshold (e.g. 13.5
    rebounds — Over means 14+, Under means 13 or fewer).
    Either pass book_prices (dict book -> (over, under) at this
    line) to build the consensus anchor, or fair_prob_override
    with anchor_label explaining where it came from (used only
    for the clearly-labelled sample run).
    If the anchor is for the Over side and side is Under, the
    fair Under prob is 1 - fair Over prob.
    fanduel_american: FanDuel's price for the SAME side at the
    SAME line, checked against the same anchor (FanDuel is
    excluded from the anchor). Its EV is reported alongside
    DraftKings'; the dashboard highlights the better of the two.
    """
    if book_prices:
        fair_over, n_books, anchor_books, flag = consensus_fair_prob(
            book_prices, method=method)
        anchor = f"consensus ({n_books} books: {', '.join(anchor_books)})"
    elif fair_prob_override is not None:
        # fair_prob_override is the fair prob for the SIDE given.
        fair_over = (float(fair_prob_override) if side.lower() == "over"
                     else 1.0 - float(fair_prob_override))
        n_books, anchor_books = 0, []
        flag = anchor_label
        anchor = anchor_label
    else:
        return {
            "game": game, "player": player, "market": market,
            "side": side, "line": line,
            "draftkings_american": draftkings_american,
            "anchor": "none", "anchor_books": "",
            "fair_prob": None, "fair_decimal": None,
            "fair_american": None, "ev_pct": None,
            "kelly_full_stake": None, "kelly_quarter_stake": None,
            "flag": "no sharp anchor — consensus only",
            "fanduel_american": ("" if fanduel_american in (None, "")
                                else float(fanduel_american)),
            "fanduel_decimal": None,
            "fanduel_implied_prob": None,
            "fanduel_ev_pct": None,
            "fanduel_flag": None,
            "method": method,
        }

    if fair_over is None:
        return {
            "game": game, "player": player, "market": market,
            "side": side, "line": line,
            "draftkings_american": draftkings_american,
            "anchor": anchor, "anchor_books": ", ".join(anchor_books),
            "fair_prob": None, "fair_decimal": None,
            "fair_american": None, "ev_pct": None,
            "kelly_full_stake": None, "kelly_quarter_stake": None,
            "flag": "no sharp anchor — consensus only",
            "fanduel_american": ("" if fanduel_american in (None, "")
                                else float(fanduel_american)),
            "fanduel_decimal": None,
            "fanduel_implied_prob": None,
            "fanduel_ev_pct": None,
            "fanduel_flag": None,
            "method": method,
        }

    fair_side = fair_over if side.lower() == "over" else 1.0 - fair_over
    dk_dec = american_to_decimal(draftkings_american)
    ev = ev_pct(draftkings_american, fair_side)
    kelly_full = kelly_stake(bankroll, fair_side, draftkings_american,
                             fraction=1.0)
    kelly_quarter = kelly_stake(bankroll, fair_side, draftkings_american,
                                fraction=0.25)
    # "+EV >=2%" is reserved for sharp anchors only. A soft or
    # single-book consensus may show a positive EV on the board,
    # but it is labelled with its anchor and never alerted.
    sharp_anchor = flag in ("sharp anchor", "thin sharp anchor")
    if sharp_anchor and ev >= 2.0:
        flag_out = "+EV >=2%"
    elif ev > 0:
        flag_out = "+EV" if sharp_anchor else f"+EV ({flag})"
    else:
        flag_out = "no edge" if sharp_anchor else flag

    # --- FanDuel, same anchor, same line ---------------------------
    fd_ev = None
    fd_flag = None
    fd_dec = None
    fd_implied = None
    if fanduel_american is not None and fanduel_american != "":
        fd_am = float(fanduel_american)
        fd_dec = american_to_decimal(fd_am)
        fd_implied = 1.0 / fd_dec
        fd_ev = ev_pct(fd_am, fair_side)
        fd_flag = ("+EV >=2%" if fd_ev >= 2.0 and sharp_anchor
                   else ("+EV" if fd_ev > 0 else "no edge"))

    return {
        "game": game,
        "player": player,
        "market": market,
        "side": side,
        "line": line,
        "draftkings_american": draftkings_american,
        "draftkings_decimal": round(dk_dec, 3),
        "draftkings_implied_prob": round(1.0 / dk_dec, 4),
        "anchor": anchor,
        "anchor_books": ", ".join(anchor_books),
        "fair_prob": round(fair_side, 4),
        "fair_decimal": round(fair_price_from_prob(fair_side), 3),
        "fair_american": round(decimal_to_american(
            fair_price_from_prob(fair_side)), 0),
        "ev_pct": round(ev, 2),
        "kelly_full_stake": round(kelly_full, 2),
        "kelly_quarter_stake": round(kelly_quarter, 2),
        "flag": flag_out,
        "fanduel_american": ("" if fanduel_american in (None, "")
                            else float(fanduel_american)),
        "fanduel_decimal": None if fd_dec is None else round(fd_dec, 3),
        "fanduel_implied_prob": (None if fd_implied is None
                                 else round(fd_implied, 4)),
        "fanduel_ev_pct": None if fd_ev is None else round(fd_ev, 2),
        "fanduel_flag": fd_flag,
        "method": method,
    }
