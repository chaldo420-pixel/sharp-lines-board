#!/usr/bin/env python3
"""Slim a getDashboard payload into the public shared-board board.json.

Usage: slim_board.py <getDashboard-output.json> <board.json-out>
Only the fields the public page needs are kept. No API keys, no links.
Anchor strings are parsed into a plain book list.
"""
import json, re, sys
from datetime import datetime, timezone

def anchor_books(anchor):
    if not anchor:
        return []
    if anchor == "pinnacle-novig":
        return ["Pinnacle (no-vig)"]
    m = re.search(r"\((\d+) books?: (.+)\)", anchor)
    if m:
        return [b.strip() for b in m.group(2).split(",")]
    return [anchor]

def slim_row(r):
    return {
        "game": r.get("game"),
        "gameDate": r.get("gameDate"),
        "kind": r.get("kind"),
        "player": r.get("player"),
        "market": r.get("market"),
        "side": r.get("side"),
        "line": r.get("line"),
        "dk": r.get("dkAmerican"),
        "fd": r.get("fdAmerican"),
        "dkEv": r.get("evPct"),
        "fdEv": r.get("fdEvPct"),
        "bestBook": r.get("bestBook"),
        "bestEv": r.get("bestEvPct"),
        "fairProb": r.get("fairProb"),
        "anchor": anchor_books(r.get("anchor")),
        "anchorDisplay": (anchor_books(r.get("anchorDisplay"))
                          if r.get("anchorDisplay") not in (None, "", "none")
                          else []),
        "flag": r.get("flag"),
    }

def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        payload = json.load(f)
    data = payload.get("data", payload)
    rows = data.get("rows", [])
    snap = data.get("snapshot", {}) or {}
    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshotLabel": snap.get("label") or data.get("lastRefresh", {}).get("etLabel", ""),
        "snapshotAt": snap.get("snapshotAt", ""),
        "rowCount": len(rows),
        "thresholdPct": (data.get("settings", {}) or {}).get("alertThresholdPct", 2),
        "rows": [slim_row(r) for r in rows],
    }
    with open(dst, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {dst}: {len(rows)} rows, {len(json.dumps(out))} bytes")

if __name__ == "__main__":
    main()
