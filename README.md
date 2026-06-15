# traffic-stats

This is a **data-only branch**, not part of the app. It exists because GitHub's
[Traffic API](https://docs.github.com/rest/metrics/traffic) only retains the
**last 14 days** of clone/view data — anything older is gone forever.

The [`traffic-stats` workflow](../../blob/main/.github/workflows/traffic.yml) on
`main` runs daily, fetches the current window, and appends it here so the full
history accumulates.

| file | contents |
|---|---|
| `clones.csv` | one row per day: `date,count,uniques` (git clones) |
| `views.csv` | one row per day: page views (created on first scheduled run) |
| `SUMMARY.md` | auto-generated cumulative totals |

`clones.csv` is seeded with 2026-05-31 → 2026-06-13, captured manually before
those days rolled out of the 14-day window.
