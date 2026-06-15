#!/usr/bin/env python3
"""Append GitHub clone (and view) traffic to CSV files.

GitHub's Traffic API only keeps the last 14 days, so anything older is gone
forever unless it's snapshotted. This script merges today's API response into
per-day CSVs (one row per date) so the full history accumulates over time.

Run by .github/workflows/traffic.yml. Env: REPO ("owner/name"), GH_PAT.
Usage: track_traffic.py <data_dir>
"""
import csv
import json
import os
import sys
import urllib.request

REPO = os.environ["REPO"]
TOKEN = os.environ["GH_PAT"]
DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else "."


def api(path):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/traffic/%s" % (REPO, path),
        headers={
            "Authorization": "token %s" % TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "claude-overlay-traffic-tracker",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def merge(filename, rows):
    """Merge API rows into <filename>, keyed by date, keeping the max seen.

    The trailing (today) bucket grows during the day, and GitHub revises recent
    days, so taking max() per date avoids ever shrinking a recorded count.
    """
    path = os.path.join(DATA_DIR, filename)
    history = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                history[row["date"]] = {
                    "date": row["date"],
                    "count": int(row["count"]),
                    "uniques": int(row["uniques"]),
                }
    for item in rows:
        date = item["timestamp"][:10]
        count, uniques = int(item["count"]), int(item["uniques"])
        prev = history.get(date)
        if prev:
            count = max(count, prev["count"])
            uniques = max(uniques, prev["uniques"])
        history[date] = {"date": date, "count": count, "uniques": uniques}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "count", "uniques"])
        w.writeheader()
        for date in sorted(history):
            w.writerow(history[date])
    total = sum(r["count"] for r in history.values())
    return total, len(history)


clones = api("clones")
views = api("views")
clone_total, clone_days = merge("clones.csv", clones.get("clones", []))
view_total, view_days = merge("views.csv", views.get("views", []))

with open(os.path.join(DATA_DIR, "SUMMARY.md"), "w", encoding="utf-8") as f:
    f.write("# Traffic history for %s\n\n" % REPO)
    f.write(
        "_Auto-updated daily. GitHub's Traffic API only retains the last 14 "
        "days; this branch preserves the full history._\n\n"
    )
    f.write("| metric | value |\n|---|---|\n")
    f.write("| Cumulative clones (sum of daily totals) | **%d** |\n" % clone_total)
    f.write("| Days of clone data on record | %d |\n" % clone_days)
    f.write("| Cumulative page views | %d |\n" % view_total)
    f.write(
        "\n> Note: \"cumulative clones\" sums each day's clone count. Per-day "
        "`uniques` cannot be summed into an all-time unique-people figure "
        "(someone who clones on two days is two daily-uniques). See "
        "`clones.csv` for the raw daily series.\n"
    )

print("clones.csv: %d days, cumulative count=%d" % (clone_days, clone_total))
print("views.csv:  %d days, cumulative count=%d" % (view_days, view_total))
