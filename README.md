# Sharp Lines — live board

Public, PIN-gated NBA odds board. Game lines compare DraftKings and
FanDuel against the Pinnacle no-vig fair price; player props
(straight main over/under lines only) compare against a
sharp-book consensus anchor. Rows at +2% EV or better are the
+EV plays.

## How it stays fresh

`.github/workflows/refresh.yml` runs every 15 minutes, 08:00–23:00 ET,
fetches PropLine, rebuilds `board.json`, commits it, and deploys
this site to GitHub Pages. No manual step, no approval tap.

## Files

- `index.html` — the board page. PIN gate first (client-side
  SHA-256 check, casual protection only), then loads `board.json`
  from the same origin and re-fetches every 5 minutes.
- `board.json` — the latest snapshot (regenerated each run).
- `model/` — the Python model: `fetch_propline.py` (PropLine
  fetch), `model.py` (Pinnacle no-vig game lines), `props.py`
  (sharp-consensus prop anchor), `poll.py` (row builders),
  `slim_board.py` (slim board format), `build_board.py`
  (the CI entry point). Standard library only, no dependencies.
- `requests_log.jsonl` — daily PropLine call counter (free tier:
  1,000 requests/day; the refresh stops at 850).

## Secrets

`PROPLINE_API_KEY` — repo secret (Settings → Secrets and
variables → Actions). The key is never committed, never logged,
never in the page. If it is missing, the workflow fails loudly
instead of publishing a stale board.
