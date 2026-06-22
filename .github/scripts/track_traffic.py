#!/usr/bin/env python3
"""Append GitHub clone (and view) traffic to CSV files.

GitHub's Traffic API only keeps the last 14 days, so anything older is gone
forever unless it's snapshotted. This script:

  * merges the per-day series into clones.csv / views.csv (one row per date), and
  * records the API's *window-level* totals (incl. the 14-day UNIQUE counts that
    GitHub computes itself) into rolling_14d.csv, one row per snapshot day.

Why the second file: a true all-time unique-cloner count is impossible -- GitHub
exposes no cloner identity, only pre-aggregated counts, so per-day uniques cannot
be de-duplicated across days. The 14-day window's `uniques` field is the best
GitHub-blessed "unique people" figure; tracking it daily gives a rolling series.

Run by .github/workflows/traffic.yml. Env: REPO ("owner/name"), GH_PAT.
Usage: track_traffic.py <data_dir>
"""
import csv
import datetime
import os
import sys
import json
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
    """Merge per-day API rows into <filename>, keyed by date, keeping the max seen.

    The trailing (today) bucket grows during the day, and GitHub revises recent
    days, so taking max() per date avoids ever shrinking a recorded count.
    Returns (sum_of_daily_count, sum_of_daily_uniques, num_days).
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
    uniq_sum = sum(r["uniques"] for r in history.values())
    return total, uniq_sum, len(history)


def record_window(filename, clones_obj, views_obj):
    """Append today's 14-day rolling totals (incl. window-level UNIQUE counts).

    One row per snapshot date; a same-day re-run keeps the max. Returns the dict.
    """
    path = os.path.join(DATA_DIR, filename)
    fields = ["snapshot_date", "clones_14d", "clone_uniques_14d",
              "views_14d", "view_uniques_14d"]
    rows = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["snapshot_date"]] = {k: int(row[k]) if k != "snapshot_date"
                                              else row[k] for k in fields}
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    new = {
        "snapshot_date": today,
        "clones_14d": int(clones_obj.get("count", 0)),
        "clone_uniques_14d": int(clones_obj.get("uniques", 0)),
        "views_14d": int(views_obj.get("count", 0)),
        "view_uniques_14d": int(views_obj.get("uniques", 0)),
    }
    prev = rows.get(today)
    if prev:
        for k in fields[1:]:
            new[k] = max(new[k], prev[k])
    rows[today] = new
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    return rows


clones = api("clones")
views = api("views")
clone_total, clone_uniq_sum, clone_days = merge("clones.csv", clones.get("clones", []))
view_total, view_uniq_sum, view_days = merge("views.csv", views.get("views", []))
windows = record_window("rolling_14d.csv", clones, views)

latest_date = max(windows)
latest_clone_uniques = windows[latest_date]["clone_uniques_14d"]
latest_view_uniques = windows[latest_date]["view_uniques_14d"]
peak_clone_uniques = max(r["clone_uniques_14d"] for r in windows.values())

with open(os.path.join(DATA_DIR, "SUMMARY.md"), "w", encoding="utf-8") as f:
    f.write("# Traffic history for %s\n\n" % REPO)
    f.write(
        "_Auto-updated daily. GitHub's Traffic API only retains the last 14 "
        "days; this branch preserves the full history._\n\n"
    )
    f.write("| metric | value |\n|---|---|\n")
    f.write("| **Unique cloners, last 14 days** (rolling, as of %s) | **%d** |\n"
            % (latest_date, latest_clone_uniques))
    f.write("| Peak 14-day unique cloners on record | %d |\n" % peak_clone_uniques)
    f.write("| Total clone events, all-time (sum of daily counts) | **%d** |\n" % clone_total)
    f.write("| Days of clone data on record | %d |\n" % clone_days)
    f.write("| Unique visitors, last 14 days (rolling) | %d |\n" % latest_view_uniques)
    f.write("| Total page views, all-time | %d |\n" % view_total)
    f.write(
        "\n> **On unique counts.** A true *all-time* unique-cloner number is not "
        "obtainable: GitHub exposes no cloner identity, only pre-aggregated counts, "
        "so per-day uniques cannot be de-duplicated across days. "
        "\"Unique cloners, last 14 days\" is GitHub's own window-level figure (the "
        "best unique metric available); its history is in `rolling_14d.csv`. "
        "For reference, the naive sum of daily unique cloners is %d, which "
        "*over*-counts (a person who clones on two days is two daily-uniques) and "
        "is only an upper bound. Raw daily series: `clones.csv`.\n"
        % clone_uniq_sum
    )

print("clones.csv: %d days, all-time count=%d, daily-uniques-sum=%d"
      % (clone_days, clone_total, clone_uniq_sum))
print("rolling_14d.csv: %d snapshots, latest unique cloners(14d)=%d, peak=%d"
      % (len(windows), latest_clone_uniques, peak_clone_uniques))
