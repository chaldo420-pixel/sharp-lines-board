"""
sharp-lines-model-v1 — model.py
Pure functions, no network, no I/O, no betting.

Idea
----
Use a sharp book's price as the fair-value anchor, strip its margin
(vig) out, and compare DraftKings' price at the SAME line/handicap.
If DraftKings pays more than the fair price, the bet has positive
expected value *against the sharp anchor* — not a guarantee. The
sharp close is a proxy for the true probability, not the truth.

Methods
-------
No-vig removal, two methods (choose with `method=`):
  * "multiplicative" (default): fair_i = q_i / sum(q), where q_i are
    the implied probabilities from the sharp book's two prices.
    Simple, transparent, standard.
  * "power": find exponent k > 0 such that sum(q_i ** k) == 1, then
    fair_i = q_i ** k. This is the "power" / Shin-style approach:
    it removes the overround by shrinking all implied probabilities
    with a single exponent instead of dividing by the overround.
    It preserves ordering, sums to 1.0, and for small overrounds
    it is very close to multiplicative. It tends to move
    longshots slightly less than multiplicative does. Both are
    approximations; report which one you used.

EV, CLV, Kelly
--------------
  EV%  = (decimal_DK * fair_prob - 1) * 100
  CLV% = (decimal_bet / decimal_close_no_vig - 1) * 100
         (bet price vs Pinnacle no-vig CLOSE; positive = you beat
          the sharp close)
  Kelly fraction f* = (b*p - q) / b, b = decimal - 1, p = fair prob,
  q = 1 - p. f* <= 0 when EV <= 0. Stakes are capped (default
  2% of bankroll for a full-Kelly stake, 0.5% for 1/4 Kelly is
  applied via the fraction, see kelly_stake).

Spreads / totals — the line-difference rule
--------------------------------------------
A spread/total is only the same bet at the same handicap.
If |Pinnacle handicap - DraftKings handicap| >= 0.5, the row is
flagged "line-differs" and EV is NOT computed as if it were the
same bet. We do not estimate a fair-price shift for a different
handicap in v1 (that would need a points-distribution model),
except that compare_row() documents the size of the difference
so a human can judge it. Moneylines have no handicap.
"""

from __future__ import annotations

import math

# ------------------------------------------------------------------
# Odds conversion
# ------------------------------------------------------------------

def american_to_decimal(american: float) -> float:
    """American odds -> decimal odds. +150 -> 2.5, -150 -> 1.6667."""
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be 0")
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def decimal_to_american(decimal: float) -> float:
    """Decimal odds -> American odds. 2.5 -> +150, 1.6667 -> -150."""
    d = float(decimal)
    if d <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    if d >= 2.0:
        return (d - 1.0) * 100.0
    return -100.0 / (d - 1.0)


def implied_prob(american: float) -> float:
    """Implied probability (including vig) from American odds."""
    return 1.0 / american_to_decimal(american)


def fair_price_from_prob(prob: float) -> float:
    """Fair decimal price for a probability, e.g. 0.5 -> 2.0."""
    p = float(prob)
    if not 0.0 < p < 1.0:
        raise ValueError("probability must be in (0, 1)")
    return 1.0 / p


# ------------------------------------------------------------------
# No-vig removal
# ------------------------------------------------------------------

def no_vig_multiplicative(implied_probs):
    """fair_i = q_i / sum(q). Sums to 1.0 by construction."""
    qs = [float(q) for q in implied_probs]
    if any(q <= 0 for q in qs):
        raise ValueError("implied probabilities must be > 0")
    total = sum(qs)
    return [q / total for q in qs]


def _power_exponent(implied_probs, tol=1e-12, lo=0.01, hi=20.0):
    """Find k with sum(q_i ** k) == 1, by bisection.

    sum(q**k) is decreasing in k for 0 < q < 1, so bisection works.
    k > 1 when there is overround (sum q > 1); k < 1 if sum q < 1.
    """
    qs = [float(q) for q in implied_probs]

    def s(k):
        return sum(q ** k for q in qs)

    # s(lo) > 1 and s(hi) < 1 for any normal two-way market.
    if s(lo) < 1.0:
        return lo
    if s(hi) > 1.0:
        return hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if s(mid) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def no_vig_power(implied_probs):
    """Power-method no-vig: fair_i = q_i ** k, k chosen so sum = 1."""
    qs = [float(q) for q in implied_probs]
    if any(not 0.0 < q < 1.0 for q in qs):
        raise ValueError("implied probabilities must be in (0, 1)")
    k = _power_exponent(qs)
    return [q ** k for q in qs]


def no_vig(implied_probs, method="multiplicative"):
    """Remove vig from a list of implied probabilities.

    method: "multiplicative" (default) or "power".
    Returns fair probabilities summing to 1.0.
    """
    if method == "multiplicative":
        return no_vig_multiplicative(implied_probs)
    if method == "power":
        return no_vig_power(implied_probs)
    raise ValueError(f"unknown no-vig method: {method!r} "
                     "(use 'multiplicative' or 'power')")


def no_vig_pair(pinnacle_a_american, pinnacle_b_american, method="multiplicative"):
    """Two-way sharp market -> (fair_prob_a, fair_prob_b).

    a/b are the two sides of the same market (e.g. home/away ML,
    over/under at the same total, -spread/+spread at the same
    handicap). Returns fair probabilities in the same order.
    """
    qa = implied_prob(pinnacle_a_american)
    qb = implied_prob(pinnacle_b_american)
    fa, fb = no_vig([qa, qb], method=method)
    return fa, fb


# ------------------------------------------------------------------
# EV / CLV / Kelly
# ------------------------------------------------------------------

def ev_pct(draftkings_american: float, fair_prob: float) -> float:
    """EV% of a DraftKings bet vs a fair probability.

    EV% = (decimal_DK * fair_prob - 1) * 100.
    ~0 for a fair bet, > 0 when DK pays more than fair.
    """
    dec = american_to_decimal(draftkings_american)
    return (dec * float(fair_prob) - 1.0) * 100.0


def clv_pct(bet_american: float, close_no_vig_american: float) -> float:
    """Closing-line value % vs a no-vig close.

    CLV% = (decimal_bet / decimal_close_no_vig - 1) * 100.
    Positive = you beat the sharp close. Requires the close to be
    a no-vig price (fair price), not a vigged price.
    """
    return (american_to_decimal(bet_american)
            / american_to_decimal(close_no_vig_american) - 1.0) * 100.0


def kelly_fraction(fair_prob: float, draftkings_american: float) -> float:
    """Full Kelly fraction of bankroll. 0.0 when EV <= 0.

    f* = (b*p - q) / b, b = decimal - 1, p = fair prob, q = 1 - p.
    """
    p = float(fair_prob)
    dec = american_to_decimal(draftkings_american)
    b = dec - 1.0
    if b <= 0:
        return 0.0
    f = (b * p - (1.0 - p)) / b
    return max(0.0, f)


def kelly_stake(bankroll: float, fair_prob: float,
                draftkings_american: float,
                fraction: float = 0.25, cap_frac: float = 0.02) -> float:
    """Stake in currency units for a Kelly fraction, capped.

    fraction=0.25 is 1/4 Kelly (the default here). cap_frac caps
    the stake at cap_frac * bankroll (default 2% of bankroll),
    even if full Kelly says more. Returns 0.0 when EV <= 0.
    """
    if bankroll <= 0:
        raise ValueError("bankroll must be > 0")
    f = kelly_fraction(fair_prob, draftkings_american)
    stake = bankroll * f * float(fraction)
    return min(stake, bankroll * float(cap_frac))


# ------------------------------------------------------------------
# One-row comparison (game lines)
# ------------------------------------------------------------------

def compare_row(game: str, market: str, side: str,
                pinnacle_a_american: float, pinnacle_b_american: float,
                draftkings_american: float,
                pinnacle_handicap=None, draftkings_handicap=None,
                fanduel_american=None, fanduel_handicap=None,
                method: str = "multiplicative",
                bankroll: float = 1000.0):
    """Compare DraftKings AND FanDuel against a Pinnacle two-way market.

    pinnacle_a/b are the two sides of the SAME Pinnacle market;
    the row being priced is the 'a' side (fair_prob is for side a).
    For a spread/total row, pinnacle_handicap / draftkings_handicap
    / fanduel_handicap are the handicaps; if a book's handicap
    differs from Pinnacle's by >= 0.5 that book's row is flagged
    'line-differs' and its EV is None — do not treat it as the
    same bet. Each book is flagged separately, so one book can be
    line-differs while the other computes an EV. For moneylines
    pass handicap=None for all books.

    "flag" is the DraftKings outcome (kept for backward
    compatibility); fanduel_flag is FanDuel's own outcome.
    Returns a dict with fair prob/price, both books' implied
    prob, EV%, Kelly stakes, and flags.
    """
    fair_a, _fair_b = no_vig_pair(pinnacle_a_american,
                                  pinnacle_b_american, method=method)
    dk_dec = american_to_decimal(draftkings_american)
    dk_implied = 1.0 / dk_dec

    line_differs = False
    handicap_diff = None
    if pinnacle_handicap is not None and draftkings_handicap is not None:
        handicap_diff = abs(float(pinnacle_handicap)
                            - float(draftkings_handicap))
        line_differs = handicap_diff >= 0.5

    if line_differs:
        ev = None
        kelly_full = None
        kelly_quarter = None
        flag = "line-differs"
    else:
        ev = ev_pct(draftkings_american, fair_a)
        kelly_full = kelly_stake(bankroll, fair_a, draftkings_american,
                                 fraction=1.0)
        kelly_quarter = kelly_stake(bankroll, fair_a, draftkings_american,
                                    fraction=0.25)
        flag = "+EV >=2%" if ev >= 2.0 else ("+EV" if ev > 0 else "no edge")

    # --- FanDuel, same anchor, flagged separately ------------------
    fd_ev = None
    fd_flag = None
    fd_dec = None
    fd_implied = None
    fd_handicap_diff = None
    if fanduel_american is not None and fanduel_american != "":
        fd_american = float(fanduel_american)
        fd_dec = american_to_decimal(fd_american)
        fd_implied = 1.0 / fd_dec
        fd_differs = False
        if (pinnacle_handicap is not None
                and fanduel_handicap is not None
                and fanduel_handicap != ""):
            fd_handicap_diff = abs(float(pinnacle_handicap)
                                   - float(fanduel_handicap))
            fd_differs = fd_handicap_diff >= 0.5
        if fd_differs:
            fd_flag = "line-differs"
        else:
            fd_ev = ev_pct(fd_american, fair_a)
            fd_flag = ("+EV >=2%" if fd_ev >= 2.0
                       else ("+EV" if fd_ev > 0 else "no edge"))

    return {
        "game": game,
        "market": market,
        "side": side,
        "handicap": draftkings_handicap,
        "pinnacle_handicap": pinnacle_handicap,
        "pinnacle_american": pinnacle_a_american,
        "pinnacle_fair_prob": round(fair_a, 4),
        "pinnacle_fair_decimal": round(fair_price_from_prob(fair_a), 3),
        "pinnacle_fair_american": round(decimal_to_american(
            fair_price_from_prob(fair_a)), 0),
        "draftkings_american": draftkings_american,
        "draftkings_decimal": round(dk_dec, 3),
        "draftkings_implied_prob": round(dk_implied, 4),
        "ev_pct": None if ev is None else round(ev, 2),
        "kelly_full_stake": None if kelly_full is None else round(kelly_full, 2),
        "kelly_quarter_stake": None if kelly_quarter is None else round(kelly_quarter, 2),
        "handicap_diff": handicap_diff,
        "flag": flag,
        "fanduel_american": ("" if fanduel_american in (None, "")
                            else float(fanduel_american)),
        "fanduel_decimal": None if fd_dec is None else round(fd_dec, 3),
        "fanduel_implied_prob": (None if fd_implied is None
                                 else round(fd_implied, 4)),
        "fanduel_ev_pct": None if fd_ev is None else round(fd_ev, 2),
        "fanduel_handicap_diff": fd_handicap_diff,
        "fanduel_flag": fd_flag,
        "method": method,
    }
