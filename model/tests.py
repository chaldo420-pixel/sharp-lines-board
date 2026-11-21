"""Unit tests for model.py and props.py. Run: python3 tests.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_board import (_apply_flips, _is_sharp_anchored,
                         _load_flagged_today, _qualifies, line_key)
from model import (american_to_decimal, clv_pct, compare_row,
                   decimal_to_american, ev_pct, kelly_fraction,
                   kelly_stake, no_vig)
from poll import new_alerts
from props import compare_prop, consensus_fair_prob


class TestConversions(unittest.TestCase):
    def test_american_decimal_roundtrip(self):
        self.assertAlmostEqual(american_to_decimal(150), 2.5)
        self.assertAlmostEqual(american_to_decimal(-150), 1.6667, places=3)
        self.assertAlmostEqual(american_to_decimal(-110), 1.9091, places=3)
        self.assertAlmostEqual(decimal_to_american(2.5), 150.0)
        self.assertAlmostEqual(decimal_to_american(1.9091), -110.0, places=0)
        self.assertAlmostEqual(decimal_to_american(
            american_to_decimal(-216)), -216.0, places=0)

    def test_zero_american_raises(self):
        with self.assertRaises(ValueError):
            american_to_decimal(0)


class TestNoVig(unittest.TestCase):
    def test_multiplicative_sums_to_one(self):
        # Pinnacle ML -210 / +190: implied 0.6774 + 0.3448 = 1.0222 overround
        fair = no_vig([1 / 2.10, 1 / 2.90], method="multiplicative")
        self.assertAlmostEqual(sum(fair), 1.0, places=9)
        self.assertGreater(fair[0], fair[1])  # favourite stays favourite

    def test_power_sums_to_one(self):
        fair = no_vig([1 / 2.10, 1 / 2.90], method="power")
        self.assertAlmostEqual(sum(fair), 1.0, places=9)
        self.assertGreater(fair[0], fair[1])

    def test_both_methods_agree_closely_on_small_overround(self):
        m = no_vig([0.52, 0.51], method="multiplicative")
        p = no_vig([0.52, 0.51], method="power")
        for a, b in zip(m, p):
            self.assertAlmostEqual(a, b, delta=0.01)


class TestEVKellyCLV(unittest.TestCase):
    def test_fair_bet_ev_about_zero(self):
        # DK pays exactly the fair price: EV ~ 0
        self.assertAlmostEqual(ev_pct(100, 0.5), 0.0, places=6)
        self.assertAlmostEqual(ev_pct(-110, 0.5238), 0.0, delta=0.5)

    def test_ev_positive_when_dk_beats_fair(self):
        # Fair prob 50% (fair +100), DK pays +120 -> EV +10%
        self.assertAlmostEqual(ev_pct(120, 0.5), 10.0, places=6)
        self.assertLess(ev_pct(-150, 0.5), 0.0)

    def test_kelly_zero_when_ev_nonpositive(self):
        self.assertEqual(kelly_fraction(0.5, -150), 0.0)
        self.assertEqual(kelly_fraction(0.4, 100), 0.0)
        self.assertEqual(kelly_stake(1000, 0.5, -150), 0.0)

    def test_kelly_positive_when_ev_positive(self):
        f = kelly_fraction(0.6, 100)  # fair 60%, DK even money
        self.assertAlmostEqual(f, 0.2, places=6)
        # 1/4 Kelly on $1000, capped at 2% ($20): 1000*0.2*0.25 = $50 -> capped $20
        self.assertEqual(kelly_stake(1000, 0.6, 100, fraction=0.25), 20.0)

    def test_clv(self):
        # Bet at +120, close no-vig +100 -> CLV +10%
        self.assertAlmostEqual(clv_pct(120, 100), 10.0, places=6)
        self.assertAlmostEqual(clv_pct(100, 120), -9.09, places=2)


class TestCompareRow(unittest.TestCase):
    def test_spread_line_difference_flag(self):
        # Pinnacle total 224.5, DK total 225.5: different bet, EV must be None
        r = compare_row("G", "total", "Over 224.5", -106, -106, -110,
                        pinnacle_handicap=224.5, draftkings_handicap=225.5)
        self.assertEqual(r["flag"], "line-differs")
        self.assertIsNone(r["ev_pct"])
        self.assertIsNone(r["kelly_quarter_stake"])
        self.assertEqual(r["handicap_diff"], 1.0)

    def test_same_line_computes_ev(self):
        # Pinnacle ML -210/+190, DK MEM -216: fair MEM prob ~0.663, DK -216 = 0.684 implied
        r = compare_row("G", "moneyline", "MEM ML", -210, 190, -216)
        self.assertIsNotNone(r["ev_pct"])
        self.assertAlmostEqual(r["pinnacle_fair_prob"], 0.6627, places=3)
        self.assertLess(r["ev_pct"], 0)  # DK -216 is worse than fair -196ish

    def test_moneyline_no_handicap_flag(self):
        r = compare_row("G", "moneyline", "GSW ML", 190, -210, 179)
        self.assertIsNone(r["handicap_diff"])
        self.assertNotEqual(r["flag"], "line-differs")


class TestProps(unittest.TestCase):
    def test_consensus_excludes_draftkings_and_fanduel(self):
        # Both checked books are excluded from the anchor (circular
        # otherwise): DK + FD + BetMGM leaves BetMGM only — n=1,
        # single-book anchor, never alertable. Fair Over from
        # BetMGM -115/-105 ≈ 0.511.
        books = {"DraftKings": (-110, -110), "FanDuel": (-110, -110),
                 "BetMGM": (-115, -105)}
        fair, n, anchor_books, flag, display = consensus_fair_prob(books)
        self.assertEqual(n, 1)
        self.assertEqual(anchor_books, ["BetMGM"])
        self.assertNotIn("DraftKings", anchor_books)
        self.assertNotIn("FanDuel", anchor_books)
        self.assertEqual(flag, "single-book anchor — do not alert")
        self.assertAlmostEqual(fair, 0.511, delta=0.05)

    def test_consensus_sharp_anchor_needs_sharp_book(self):
        # Two soft books only (BetMGM + Hard Rock) is a soft
        # consensus, not a sharp anchor — no alert, however
        # high the EV. Add Circa and it becomes a sharp anchor.
        # (Fallback rule, Nov 20 2026: the anchor is built from
        # the non-fallback core first — Hard Rock + Circa is
        # already 2 books, so BetMGM is not used as filler.)
        soft = {"BetMGM": (-115, -105), "Hard Rock Bet": (-110, -120)}
        _f, n, _b, flag, _display = consensus_fair_prob(soft)
        self.assertEqual(n, 2)
        self.assertEqual(flag, "soft consensus — do not alert")
        sharp = dict(soft, Circa=(-112, -108))
        _f, n, anchor_books, flag, _display = consensus_fair_prob(sharp)
        self.assertEqual(n, 2)
        self.assertEqual(anchor_books, ["Hard Rock Bet", "Circa"])
        self.assertEqual(flag, "thin sharp anchor")

    def test_soft_books_fallback_only_when_sharp_coverage(self):
        # Fallback rule (Nov 20 2026): BetMGM, BetRivers and
        # Fanatics are dropped from the anchor whenever 2+ sharp
        # books price the line, and kept only to reach the
        # 2-book minimum when sharp coverage is thin.
        books = {"BetMGM": (-115, -105), "BetRivers": (-112, -108),
                 "Fanatics": (-110, -110), "Kalshi": (-113, -107),
                 "Novig": (-114, -106)}
        _f, n, anchor_books, flag, _display = consensus_fair_prob(books)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(anchor_books), ["Kalshi", "Novig"])
        self.assertEqual(flag, "thin sharp anchor")
        # One sharp book only: soft books stay (thin coverage).
        thin = {"BetMGM": (-115, -105), "Kalshi": (-113, -107)}
        _f, n, anchor_books, flag, _display = consensus_fair_prob(thin)
        self.assertEqual(n, 2)
        self.assertIn("BetMGM", anchor_books)
        self.assertEqual(flag, "thin sharp anchor")

    def test_fallback_fills_only_to_two_books(self):
        # Exact filler rule: the anchor is built from the
        # non-fallback core first; soft books fill ONLY up to
        # the 2-book minimum, in canonical anchor order.
        # Core already has 2 books (no sharp): no fallback used.
        # (ProphetX is sharp since the Nov 20 2026 fitted weights,
        # so the no-sharp core here is Hard Rock + Caesars.)
        books = {"Hard Rock Bet": (-110, -120),
                 "Caesars": (-112, -108),
                 "BetMGM": (-115, -105)}
        _f, n, anchor_books, flag, _display = consensus_fair_prob(books)
        self.assertEqual(n, 2)
        self.assertNotIn("BetMGM", anchor_books)
        self.assertEqual(flag, "soft consensus — do not alert")
        # Core has 1 book, 2 fallbacks available: only 1 used,
        # the first in canonical order (BetMGM before BetRivers),
        # appended after the core book.
        books = {"Kalshi": (-113, -107), "BetMGM": (-115, -105),
                 "BetRivers": (-112, -108)}
        _f, n, anchor_books, flag, _display = consensus_fair_prob(books)
        self.assertEqual(n, 2)
        self.assertEqual(anchor_books, ["Kalshi", "BetMGM"])
        self.assertEqual(flag, "thin sharp anchor")
        # Core empty: both fallbacks used (soft consensus).
        books = {"BetMGM": (-115, -105), "Fanatics": (-110, -110)}
        _f, n, anchor_books, flag, _display = consensus_fair_prob(books)
        self.assertEqual(n, 2)
        self.assertEqual(anchor_books, ["BetMGM", "Fanatics"])

    def test_two_sharps_plus_soft_books_anchor_string(self):
        # 2+ sharps plus all three soft books: the soft books
        # must be absent from the anchor string entirely.
        r = compare_prop(
            "G", "X", "Points", "Over", 11.5, -110,
            book_prices={"Kalshi": (-110, -110),
                         "Novig": (-110, -110),
                         "BetMGM": (-110, -110),
                         "BetRivers": (-110, -110),
                         "Fanatics": (-110, -110)})
        self.assertNotIn("BetMGM", r["anchor"])
        self.assertNotIn("BetRivers", r["anchor"])
        self.assertNotIn("Fanatics", r["anchor"])
        self.assertIn("Kalshi", r["anchor"])
        self.assertIn("Novig", r["anchor"])
        self.assertEqual(r["flag"], "no edge")

    def test_soft_anchor_high_ev_never_alerts(self):
        # A soft-anchor prop can show a high EV and still never
        # alert: its flag carries the parenthetical, and the
        # alert gate rejects it.
        r = compare_prop(
            "G", "X", "Points", "Over", 11.5, 150,
            book_prices={"BetMGM": (-110, -110),
                         "Hard Rock Bet": (-110, -110)})
        self.assertGreater(r["ev_pct"], 2.0)
        self.assertEqual(r["flag"],
                         "+EV (soft consensus — do not alert)")
        self.assertFalse(new_alerts([r], {})[0])

    def test_single_sharp_book_is_not_a_sharp_anchor(self):
        # Regression (Nov 19 2026): one sharp book alone — Kalshi
        # or Novig by itself — was wrongly labelled "thin sharp
        # anchor" and flagged +EV. A single source is never an
        # anchor, sharp or not.
        for book in ("Novig", "Kalshi", "Circa"):
            _f, n, _b, flag, _display = consensus_fair_prob({book: (-110, -110)})
            self.assertEqual(n, 1)
            self.assertEqual(flag, "single-book anchor — do not alert")

    def test_consensus_no_books(self):
        fair, n, _, flag, _display = consensus_fair_prob({"DraftKings": (-110, -110)})
        self.assertIsNone(fair)
        self.assertEqual(n, 0)
        self.assertIn("no sharp anchor", flag)

    def test_display_anchor_hides_soft_filler(self):
        # Display anchor (Nov 20 2026, George): only the books
        # that actually set the fair price are shown. 1 sharp +
        # 1 soft filler: the sharp book is duplicated in the
        # median, so the median IS the sharp book's price and the
        # filler is not shown — but it stays in anchor_books (the
        # watcher still gates on the full anchor).
        _f, n, anchor_books, flag, display = consensus_fair_prob(
            {"Kalshi": (-113, -107), "BetMGM": (-115, -105)})
        self.assertEqual(n, 2)
        self.assertEqual(anchor_books, ["Kalshi", "BetMGM"])
        self.assertEqual(flag, "thin sharp anchor")
        self.assertEqual(display, ["Kalshi"])
        r = compare_prop(
            "G", "X", "Threes", "Under", 2.5, -165,
            book_prices={"Kalshi": (-113, -107),
                         "BetMGM": (-115, -105)})
        self.assertIn("BetMGM", r["anchor"])
        self.assertEqual(r["anchor_display"],
                         "consensus (1 book: Kalshi)")

    def test_display_anchor_shows_soft_book_at_median(self):
        # 1 sharp + 2 weight-1 books: when a soft book genuinely
        # sits at the median it DID move the price, so it IS shown.
        # Kalshi 0.5, Caesars ~0.457, Hard Rock ~0.438: expanded
        # [0.438, 0.457, 0.5, 0.5] -> median (0.457+0.5)/2.
        # (Bovada would also sit at 0.457, but it is excluded
        # from the anchor outright — fitted weights, Nov 20 2026.)
        _f, n, _b, flag, display = consensus_fair_prob(
            {"Kalshi": (-110, -110), "Caesars": (110, -130),
             "Hard Rock Bet": (120, -140)})
        self.assertEqual(n, 3)
        self.assertEqual(flag, "sharp anchor")
        # Display order is canonical anchor order (Kalshi before
        # Caesars), not alphabetical.
        self.assertEqual(display, ["Kalshi", "Caesars"])

    def test_display_anchor_two_and_three_sharps(self):
        # Two sharp books at the same price: both set it.
        _f, _n, _b, _flag, display = consensus_fair_prob(
            {"Kalshi": (-110, -110), "Novig": (-110, -110)})
        self.assertEqual(sorted(display), ["Kalshi", "Novig"])
        # Three sharp books, distinct prices: only the middle one.
        _f, _n, _b, _flag, display = consensus_fair_prob(
            {"Kalshi": (-110, -110), "Novig": (-130, 110),
             "Circa": (110, -130)})
        self.assertEqual(display, ["Kalshi"])

    def test_fitted_weights_prophetx_and_betonline_are_sharp(self):
        # Fitted weights (Nov 20 2026): ProphetX (realized 0.40)
        # and BetOnline (0.23) are sharp — they were weight 1
        # before, which underweighted the two sharpest measured
        # books in the feed. Each plus a soft filler is a thin
        # sharp anchor, not a soft consensus.
        for book in ("ProphetX", "BetOnline"):
            _f, n, anchor_books, flag, _d = consensus_fair_prob(
                {book: (-110, -110), "BetMGM": (-115, -105)})
            self.assertEqual(n, 2)
            self.assertIn(book, anchor_books)
            self.assertEqual(flag, "thin sharp anchor")

    def test_fitted_weights_pinnacle_votes_at_weight_3(self):
        # Pinnacle (Nov 20 2026 correction: it DOES post props,
        # best calibrated at 50.1% predicted vs 49.5% actual)
        # votes at weight 3: its price outvotes two weight-1
        # books pulling the other way. Pinnacle 0.5 vs two books
        # at ~0.625: expanded [0.5, 0.5, 0.5, 0.625, 0.625] ->
        # median 0.5, display Pinnacle only.
        books = {"Pinnacle": (-110, -110),
                 "Caesars": (-200, 150), "Hard Rock Bet": (-200, 150)}
        fair, n, anchor_books, flag, display = consensus_fair_prob(books)
        self.assertEqual(n, 3)
        self.assertEqual(flag, "sharp anchor")
        self.assertIn("Pinnacle", anchor_books)
        self.assertAlmostEqual(fair, 0.5, delta=0.01)
        self.assertEqual(display, ["Pinnacle"])

    def test_fitted_weights_bovada_excluded_from_anchor(self):
        # Bovada is the softest book in the feed (realized 1.10,
        # highest avgClv) — it is out of the anchor entirely, not
        # even as filler. Bovada + a sharp book leaves a
        # single-book anchor; Bovada + a fallback leaves only
        # the fallback.
        _f, n, anchor_books, flag, _d = consensus_fair_prob(
            {"Bovada": (-110, -110), "Kalshi": (-110, -110)})
        self.assertEqual(n, 1)
        self.assertEqual(anchor_books, ["Kalshi"])
        self.assertEqual(flag, "single-book anchor — do not alert")
        _f, n, anchor_books, flag, _d = consensus_fair_prob(
            {"Bovada": (-110, -110), "BetMGM": (-115, -105)})
        self.assertEqual(n, 1)
        self.assertEqual(anchor_books, ["BetMGM"])

    def test_pinnacle_prop_column_parsed(self):
        # parse_props must keep Pinnacle's prop prices (pn_over /
        # pn_under) — the feed returns them and the anchor needs
        # them at weight 3.
        from fetch_propline import parse_props
        payload = {"bookmakers": [
            {"key": "draftkings", "markets": [
                {"key": "player_points", "line_type": "main",
                 "outcomes": [
                     {"name": "Over", "description": "Test Player",
                      "point": 21.5, "price": -110},
                     {"name": "Under", "description": "Test Player",
                      "point": 21.5, "price": -110}]}]},
            {"key": "pinnacle", "markets": [
                {"key": "player_points", "line_type": "main",
                 "outcomes": [
                     {"name": "Over", "description": "Test Player",
                      "point": 21.5, "price": -115},
                     {"name": "Under", "description": "Test Player",
                      "point": 21.5, "price": -105}]}]}]}
        rows = parse_props(payload, "Test Game")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pn_over"], -115)
        self.assertEqual(rows[0]["pn_under"], -105)

    def test_prop_line_difference_is_different_bet(self):
        # compare_prop with a fair-prob override is the sample path;
        # the line itself is carried through unchanged for audit.
        r = compare_prop("G", "X", "Rebounds", "Over", 13.5, -110,
                         fair_prob_override=0.55,
                         anchor_label="sample (illustrative)")
        self.assertEqual(r["line"], 13.5)
        self.assertEqual(r["fair_prob"], 0.55)
        self.assertIn("illustrative", r["anchor"])

    def test_prop_no_anchor_ev_is_none(self):
        r = compare_prop("G", "X", "Points", "Over", 20.5, -110)
        self.assertIsNone(r["ev_pct"])
        self.assertIn("no sharp anchor", r["flag"])

    def test_main_line_consensus_ev_is_small(self):
        # Ryan Rollins Points 11.5, Nov 19 (LineSmart 6:16 PM ET):
        # DK -109/-117, Hard Rock -110/-120, FanDuel -104/-122.
        # A straight main line priced near -110 must NOT produce
        # a huge EV — a big EV here would mean a line mismatch.
        r = compare_prop(
            "POR @ MIL", "Ryan Rollins", "Points", "Over", 11.5, -109,
            book_prices={"Hard Rock Bet": (-110, -120),
                         "FanDuel": (-104, -122)})
        self.assertLess(abs(r["ev_pct"]), 10.0)
        self.assertIn("single-book anchor", r["flag"])

    def test_main_board_has_no_alt_anchor(self):
        # Regression test for the Nov 19 fix: the props board
        # must be straight main O/U on a consensus anchor.
        import csv as _csv
        path = os.path.join(os.path.dirname(__file__),
                            "props_comparison.csv")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertNotIn("illustrative", r["anchor"].lower())
            # main lines are .5 numbers near -110 pricing; alt
            # longshots (+234 to +670) must never appear here.
            self.assertLess(float(r["draftkings_american"]), 200)

    def test_compare_row_fanduel_ev(self):
        # Pinnacle -110/+110 has no overround, so fair_a = 0.5238.
        # DK -120 is -3.96% EV; FanDuel +120 is +15.24% EV. DK flag
        # must stay the DK outcome; fanduel_flag is FanDuel's own.
        r = compare_row("G", "moneyline", "X ML", -110, 110, -120,
                        fanduel_american=120)
        self.assertAlmostEqual(r["pinnacle_fair_prob"], 0.5238, places=3)
        self.assertAlmostEqual(r["fanduel_ev_pct"], 15.24, places=1)
        self.assertIn("+EV", r["fanduel_flag"])
        self.assertLess(r["ev_pct"], 0)
        self.assertNotIn("+EV", r["flag"])

    def test_compare_prop_fanduel_ev(self):
        # Hard Rock + BetMGM anchor, fair Over ~0.5: DK -110 is
        # about -4.5% EV, FanDuel +130 is about +15% EV.
        r = compare_prop(
            "G", "X", "Points", "Over", 11.5, -110,
            book_prices={"Hard Rock Bet": (-110, -110),
                         "BetMGM": (-110, -110)},
            fanduel_american=130)
        self.assertAlmostEqual(r["fanduel_ev_pct"], 15.0, delta=2.0)
        self.assertIn("+EV", r["fanduel_flag"])
        self.assertLess(r["ev_pct"], 0)


class TestPoller(unittest.TestCase):
    def test_window_8am_to_11pm_et_only(self):
        from poll import in_window
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        for hh, expected in ((7, False), (8, True), (12, True),
                             (22, True), (23, False), (2, False)):
            dt = datetime(2026, 11, 19, hh, 0, tzinfo=et)
            self.assertEqual(in_window(dt), expected, f"hour {hh}")

    def test_budget_cap_stops_before_850(self):
        import json as _json
        import tempfile
        from poll import today_total, budget_ok, BUDGET_CAP
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                        delete=False) as f:
            f.write(_json.dumps({"date": "2026-11-19", "calls": 840}) + "\n")
            path = f.name
        try:
            self.assertEqual(today_total("2026-11-19", path), 840)
            # 840 + 16 estimated = 856 > 850 -> must stop
            self.assertFalse(budget_ok("2026-11-19", 16, path))
            # 840 + 10 = 850 -> exactly at cap, allowed
            self.assertTrue(budget_ok("2026-11-19", 10, path))
            self.assertEqual(BUDGET_CAP, 850)
        finally:
            os.unlink(path)

    def test_budget_ok_allows_small_estimate_on_empty_day(self):
        from poll import budget_ok
        self.assertTrue(budget_ok("2099-01-01", 16,
                                  path="/tmp/definitely-not-a-log-xyz.jsonl"))

    def test_seen_alerts_dedup(self):
        from poll import new_alerts
        row = {"game": "G", "player": "X", "market": "Points",
               "side": "Over", "line": 11.5, "ev_pct": 3.5,
               "draftkings_american": -110, "fair_prob": 0.55,
               "anchor_books": "Circa, BetMGM"}
        seen = {}
        new1, seen = new_alerts([row], seen)
        self.assertEqual(len(new1), 1)
        new2, seen = new_alerts([row], seen)
        self.assertEqual(len(new2), 0)  # same line does not re-alert
        row2 = dict(row, line=12.5)
        new3, _ = new_alerts([row2], seen)
        self.assertEqual(len(new3), 1)  # a different line is new

    def test_new_alerts_soft_anchor_never_alerts(self):
        # Sharp-anchor rule (Nov 19 2026): a prop whose anchor
        # is soft books only — BetMGM alone, or BetMGM + Hard
        # Rock — never alerts, however high its EV.
        from poll import new_alerts
        soft = {"game": "G", "player": "X", "market": "Points",
                "side": "Over", "line": 11.5, "ev_pct": 9.0,
                "draftkings_american": -110, "fair_prob": 0.55,
                "anchor_books": "BetMGM"}
        new, _ = new_alerts([soft], {})
        self.assertEqual(len(new), 0)
        soft2 = dict(soft, anchor_books="BetMGM, Hard Rock Bet")
        new, _ = new_alerts([soft2], {})
        self.assertEqual(len(new), 0)

    def test_new_alerts_dual_book_one_alert(self):
        # Both books qualify on the same line: exactly ONE alert,
        # naming both books, best_book = the higher EV.
        from poll import new_alerts
        row = {"game": "G", "player": "X", "market": "Points",
               "side": "Over", "line": 11.5, "ev_pct": 3.0,
               "fanduel_ev_pct": 4.5,
               "draftkings_american": -110, "fanduel_american": -104,
               "fair_prob": 0.55, "anchor_books": "Circa, BetMGM"}
        seen = {}
        new, seen = new_alerts([row], seen)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["books"], ["DraftKings", "FanDuel"])
        self.assertEqual(new[0]["best_book"], "FanDuel")
        self.assertEqual(new[0]["ev_pct"], 4.5)
        self.assertEqual(new[0]["link"], "")
        self.assertEqual(new[0]["link_kind"], "")
        # Re-run: same line does not re-alert.
        new2, _ = new_alerts([row], seen)
        self.assertEqual(len(new2), 0)

    def test_new_alerts_fanduel_only(self):
        # Only FanDuel qualifies: label names FanDuel, best_book
        # is FanDuel, and there is no link at all (George, Nov 19
        # 2026: FanDuel page link removed — it did not connect
        # to the bet).
        from poll import new_alerts
        row = {"game": "G", "player": "X", "market": "Points",
               "side": "Over", "line": 11.5, "ev_pct": -4.5,
               "fanduel_ev_pct": 3.1,
               "draftkings_american": -110, "fanduel_american": -104,
               "fair_prob": 0.55, "anchor_books": "Circa, BetMGM"}
        new, _ = new_alerts([row], {})
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0]["label"].startswith("FanDuel:"))
        self.assertEqual(new[0]["best_book"], "FanDuel")
        self.assertEqual(new[0]["link"], "")
        self.assertEqual(new[0]["link_kind"], "")

    def test_dashboard_csv_fits_one_import(self):
        # Slim dashboard CSV (George, Nov 19 2026): the props
        # dashboard copy must fit ONE importcsv call (< 100,000
        # chars) on a 4-game slate, so the board loads in minutes
        # instead of ~10. The full props_comparison.csv keeps
        # every column; only the dashboard copy is slimmed.
        import csv as _csv
        from poll import DASHBOARD_PROP_FIELDS, write_dashboard_csv
        base = os.path.join(os.path.dirname(__file__),
                            "props_comparison.csv")
        with open(base, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        out = "/tmp/test-dashboard-props.csv"
        write_dashboard_csv(out, rows, DASHBOARD_PROP_FIELDS)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertLess(len(text), 100000)
        with open(out, newline="", encoding="utf-8") as f:
            slim = list(_csv.DictReader(f))
        self.assertEqual(len(slim), len(rows))
        for col in ("game", "player", "market", "side", "line",
                    "draftkings_american", "anchor", "fair_prob",
                    "ev_pct", "flag", "timestamp"):
            self.assertIn(col, slim[0])
        for col in ("clv_pct", "method", "anchor_books",
                    "kelly_full_stake", "source"):
            self.assertNotIn(col, slim[0])

    def test_book_page_links_no_links(self):
        # No bet links for either book (George, Nov 19 2026):
        # PropLine supplies no exact bet link, and the FanDuel
        # NBA page link was removed because it did not connect
        # to the bet.
        from poll import BOOK_PAGE_LINKS
        self.assertEqual(BOOK_PAGE_LINKS["DraftKings"], "")
        self.assertEqual(BOOK_PAGE_LINKS["FanDuel"], "")

    def test_game_lines_keep_main_total_only(self):
        # Regression: first live run treated every totals line
        # (alt totals, team totals) as the game total and produced
        # absurd EVs (+171%). Only line_type main + team None is
        # the game total. Second bug in the same run: DraftKings
        # uses short team names ("MIL Bucks") vs Pinnacle full
        # names, so the join must use side (home/away), not name.
        from fetch_propline import parse_game_lines
        mk = lambda key, lt, team, outcomes: {
            "key": key, "line_type": lt, "team": team, "outcomes": outcomes}
        oc = lambda name, price, point=None, side=None: {
            "name": name, "price": price, "point": point, "side": side}
        payload = {"bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": [
                mk("totals", "main", None, [
                    oc("Over", -110, 230.5), oc("Under", -104, 230.5)]),
                mk("totals", "alt", None, [
                    oc("Over", 386, 119.5), oc("Under", -500, 119.5)]),
                mk("totals", "main", "Portland Trail Blazers", [
                    oc("Over", -110, 119.5), oc("Under", -110, 119.5)]),
                mk("h2h", "main", None, [
                    oc("Milwaukee Bucks", -514, None, "home"),
                    oc("Portland Trail Blazers", 396, None, "away")]),
                mk("spreads", "main", None, [
                    oc("Milwaukee Bucks", -104, 11.5, "home"),
                    oc("Portland Trail Blazers", -108, -11.5, "away")]),
            ]},
            {"key": "draftkings", "title": "DraftKings", "markets": [
                mk("totals", "main", None, [
                    oc("Over", -112, 230.5), oc("Under", -108, 230.5)]),
                mk("totals", "alt", None, [
                    oc("Over", 386, 119.5), oc("Under", -500, 119.5)]),
                mk("h2h", "main", None, [
                    oc("MIL Bucks", -518, None, "home"),
                    oc("POR Trail Blazers", 394, None, "away")]),
                mk("spreads", "main", None, [
                    oc("MIL Bucks", -112, 11.5, "home"),
                    oc("POR Trail Blazers", -108, -11.5, "away")]),
            ]},
        ]}
        rows = parse_game_lines(payload, "POR @ MIL")
        totals = [r for r in rows if r["market"] == "total"]
        self.assertEqual(len(totals), 2)  # Over + Under 230.5 only
        self.assertTrue(all(r["side"].endswith("230.5")
                            for r in totals))
        self.assertEqual(len([r for r in rows
                              if r["market"] == "moneyline"]), 2)
        self.assertEqual(len([r for r in rows
                              if r["market"] == "spread"]), 2)
        # Labels use the full Pinnacle name, DK short name joined.
        self.assertTrue(any("Milwaukee Bucks ML" in r["side"]
                            for r in rows))

    def test_propline_parser_keeps_main_only(self):
        # Fixture shaped like a real PropLine odds payload.
        from fetch_propline import parse_props
        payload = {"bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "player_points", "line_type": "main", "outcomes": [
                    {"name": "Over", "description": "Ryan Rollins",
                     "price": -110, "point": 11.5},
                    {"name": "Under", "description": "Ryan Rollins",
                     "price": -110, "point": 11.5}]},
                {"key": "player_points", "line_type": "alt", "outcomes": [
                    {"name": "Over", "description": "Ryan Rollins",
                     "price": 660, "point": 20.0},
                    {"name": "Under", "description": "Ryan Rollins",
                     "price": -900, "point": 20.0}]}]},
            {"key": "fanduel", "title": "FanDuel", "markets": [
                {"key": "player_points", "line_type": "main", "outcomes": [
                    {"name": "Over", "description": "Ryan Rollins",
                     "price": -104, "point": 11.5},
                    {"name": "Under", "description": "Ryan Rollins",
                     "price": -122, "point": 11.5}]}]},
        ]}
        rows = parse_props(payload, "POR @ MIL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["line"], 11.5)
        self.assertEqual(rows[0]["dk_over"], -110.0)


class TestFlipMarker(unittest.TestCase):
    def _row(self, **kw):
        r = {"game": "Boston Celtics @ Sacramento Kings",
             "gameDate": "2026-11-21", "kind": "prop",
             "player": "Zach LaVine", "market": "Rebounds",
             "side": "Under", "line": 2.5, "bestEv": 4.69,
             "anchor": ["Novig", "BetRivers"]}
        r.update(kw)
        return r

    def test_line_key_format(self):
        # Watcher format: whole numbers without a decimal,
        # .5 lines as-is, null line as ''.
        self.assertEqual(
            line_key(self._row(line=3)),
            "2026-11-21|Boston Celtics @ Sacramento Kings|"
            "Zach LaVine|Rebounds|Under|3")
        self.assertEqual(
            line_key(self._row(line=2.5)),
            "2026-11-21|Boston Celtics @ Sacramento Kings|"
            "Zach LaVine|Rebounds|Under|2.5")
        self.assertEqual(
            line_key(self._row(kind="game", player=None,
                               market="moneyline",
                               side="Boston Celtics ML", line=None,
                               anchor=["Pinnacle (no-vig)"])),
            "2026-11-21|Boston Celtics @ Sacramento Kings||"
            "moneyline|Boston Celtics ML|")

    def test_same_snapshot_opposite_side_is_not_a_flip(self):
        # Old-list-only rule: both sides qualifying in the SAME
        # snapshot is not a flip — the flip needs an earlier
        # snapshot. Both sides still qualify and both enter
        # flaggedToday (no one-side suppression).
        rows = [self._row(side="Over", bestEv=3.0),
                self._row(side="Under", bestEv=4.0)]
        new = _apply_flips(rows, [], "2026-11-21")
        self.assertFalse(any(r.get("flip") for r in rows))
        self.assertEqual(len(new), 2)

    def test_opposite_side_in_old_list_marks_flip(self):
        old = ["2026-11-21|Boston Celtics @ Sacramento Kings|"
               "Zach LaVine|Rebounds|Under|2.5"]
        rows = [self._row(side="Over", bestEv=3.0)]
        new = _apply_flips(rows, old, "2026-11-21")
        self.assertTrue(rows[0].get("flip"))
        self.assertEqual(rows[0].get("flipSide"), "Under")
        self.assertIn("2026-11-21|Boston Celtics @ Sacramento Kings|"
                      "Zach LaVine|Rebounds|Over|2.5", new)
        self.assertIn(old[0], new)  # old keys carried forward

    def test_same_side_in_old_list_is_not_a_flip(self):
        old = ["2026-11-21|Boston Celtics @ Sacramento Kings|"
               "Zach LaVine|Rebounds|Over|2.5"]
        rows = [self._row(side="Over", bestEv=3.0)]
        _apply_flips(rows, old, "2026-11-21")
        self.assertFalse(rows[0].get("flip"))

    def test_game_row_flip_uses_other_team(self):
        old = ["2026-11-21|Boston Celtics @ Sacramento Kings||"
               "moneyline|Sacramento Kings ML|"]
        rows = [self._row(kind="game", player=None,
                          market="moneyline",
                          side="Boston Celtics ML", line=None,
                          bestEv=3.4, anchor=["Pinnacle (no-vig)"])]
        _apply_flips(rows, old, "2026-11-21")
        self.assertTrue(rows[0].get("flip"))
        self.assertEqual(rows[0].get("flipSide"),
                         "Sacramento Kings ML")

    def test_non_qualifying_row_never_flips_or_records(self):
        # Single-book anchor: high EV but not sharp-anchored.
        # Below-threshold EV on a sharp anchor: also out.
        old = ["2026-11-21|Boston Celtics @ Sacramento Kings|"
               "Zach LaVine|Rebounds|Under|2.5"]
        rows = [self._row(side="Over", bestEv=9.0,
                          anchor=["BetMGM"]),
                self._row(side="Over", bestEv=1.5, line=3.5)]
        new = _apply_flips(rows, old, "2026-11-21")
        self.assertFalse(any(r.get("flip") for r in rows))
        self.assertEqual(new, old)  # nothing new recorded

    def test_qualifies_needs_today_and_sharp_anchor(self):
        self.assertTrue(_qualifies(self._row(), "2026-11-21"))
        self.assertFalse(_qualifies(self._row(), "2026-11-20"))
        self.assertFalse(_qualifies(self._row(bestEv=1.9),
                                   "2026-11-21"))
        self.assertFalse(_qualifies(self._row(anchor=["BetMGM"]),
                                   "2026-11-21"))
        self.assertFalse(_qualifies(self._row(anchor=["Novig"]),
                                   "2026-11-21"))  # single book
        self.assertTrue(_qualifies(
            self._row(anchor=["Pinnacle", "Novig"]), "2026-11-21"))
        self.assertTrue(_is_sharp_anchored(
            self._row(kind="game", anchor=["Pinnacle (no-vig)"])))

    def test_load_flagged_today_date_reset(self):
        import json as _json
        import tempfile
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import build_board
        et = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 21, 10, 0, tzinfo=et)
        old = ["2026-11-20|G|P|Points|Over|11.5"]
        cases = [
            # Yesterday's snapshot: reset to [].
            ({"snapshotAt": "2026-11-20T22:00:00-05:00",
              "flaggedToday": old}, []),
            # Today's snapshot: keep the list.
            ({"snapshotAt": "2026-11-21T09:45:00-05:00",
              "flaggedToday": old}, old),
            # Missing flaggedToday field: [].
            ({"snapshotAt": "2026-11-21T09:45:00-05:00"}, []),
            # flaggedToday not a list: [].
            ({"snapshotAt": "2026-11-21T09:45:00-05:00",
              "flaggedToday": "nope"}, []),
        ]
        orig = build_board._board_path
        try:
            for board, expected in cases:
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".json", delete=False) as f:
                    _json.dump(board, f)
                    path = f.name
                build_board._board_path = lambda p=path: p
                self.assertEqual(_load_flagged_today(now), expected)
                os.unlink(path)
            # Missing file -> [].
            build_board._board_path = lambda: \
                "/tmp/definitely-not-a-board-xyz.json"
            self.assertEqual(_load_flagged_today(now), [])
            # Corrupt JSON -> [].
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False) as f:
                f.write("{not json")
                path = f.name
            build_board._board_path = lambda p=path: p
            self.assertEqual(_load_flagged_today(now), [])
            os.unlink(path)
        finally:
            build_board._board_path = orig

    def test_skipped_tick_leaves_board_untouched(self):
        # Adaptive skip: a fresh snapshot means skip_if_not_due
        # is True and build() never runs, so board.json bytes
        # (including flaggedToday) are untouched.
        import json as _json
        import tempfile
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        import build_board
        et = ZoneInfo("America/New_York")
        now = datetime(2026, 11, 21, 10, 0, tzinfo=et)
        board = {"snapshotAt": (now - timedelta(minutes=4)).isoformat(),
                 "rows": [{"game": "A @ B",
                           "gameDate": "2026-11-21"}],
                 "flaggedToday": ["2026-11-21|A @ B|P|Points|Over|11.5"]}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            _json.dump(board, f)
            path = f.name
        orig = build_board._board_path
        try:
            build_board._board_path = lambda: path
            with open(path, "rb") as f:
                before = f.read()
            self.assertTrue(build_board.skip_if_not_due(now))
            with open(path, "rb") as f:
                after = f.read()
            self.assertEqual(before, after)
        finally:
            build_board._board_path = orig
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
