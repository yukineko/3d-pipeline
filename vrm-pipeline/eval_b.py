#!/usr/bin/env python3
"""
eval_b.py — Adoption rate and edit-distance time-series aggregation (eval-B).

Reads all records from the ledger DB and outputs a JSON report with:
- Overall adoption rate and average pHash edit distance
- Time-series windows (window-days granularity)
- Top adopted/rejected tag frequency by tag key

Usage:
    python eval_b.py \\
        [--db-path ~/.vrm-pipeline/ledger.db] \\
        [--window-days 7] \\
        [--output eval_b.json]   # omit for stdout
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_records(db_path):
    """Load (timestamp, outcome, derived) rows from the ledger DB.

    Returns an empty list if the DB does not exist (caller handles gracefully).
    """
    import sqlite3

    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestamp, outcome, derived FROM records ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_outcome(outcome_str):
    """Return (adopted: bool|None, edit_dist_phash: float|None)."""
    try:
        o = json.loads(outcome_str or '{}')
        adopted = o.get('adopted', None)   # true / false / null
        edit_dist = o.get('edit_dist_phash', None)
        return adopted, edit_dist
    except Exception:
        return None, None


def extract_tags(derived_str):
    """Return the tags dict from the derived JSON column.

    The ledger stores derived as:
      {"tag": {"tags": {...tag_key: tag_value...}, "model": "...", ...}}

    Falls back to the `tag` object itself if `tags` sub-key is absent.
    Returns {} when absent or unparseable.
    """
    try:
        d = json.loads(derived_str or '{}')
        tag = d.get('tag', {})
        return tag.get('tags', tag)
    except Exception:
        return {}


def parse_timestamp(ts_str):
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime (UTC).

    Returns None on failure.
    """
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_report():
    """Return a zeroed-out report for the DB-missing / no-records case."""
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_records": 0,
        "overall": {
            "adoption_rate": 0.0,
            "avg_edit_dist_phash": None,
            "adopted_count": 0,
            "rejected_count": 0,
        },
        "windows": [],
        "top_adopted_tags": {},
        "top_rejected_tags": {},
    }


def build_report(rows, window_days):
    """Aggregate rows into a report dict."""

    if not rows:
        return _empty_report()

    # ---- Parse all rows ------------------------------------------------
    parsed = []
    for ts_str, outcome_str, derived_str in rows:
        dt = parse_timestamp(ts_str)
        adopted, edit_dist = parse_outcome(outcome_str)
        tags = extract_tags(derived_str)
        parsed.append({
            "dt": dt,
            "adopted": adopted,
            "edit_dist": edit_dist,
            "tags": tags,
        })

    total_records = len(parsed)

    # ---- Overall stats -------------------------------------------------
    adopted_count = sum(1 for r in parsed if r["adopted"] is True)
    rejected_count = sum(1 for r in parsed if r["adopted"] is False)
    edit_dists = [r["edit_dist"] for r in parsed if r["edit_dist"] is not None]
    avg_edit_dist = (sum(edit_dists) / len(edit_dists)) if edit_dists else None
    adoption_rate = (adopted_count / total_records) if total_records > 0 else 0.0

    # ---- Time-series windows -------------------------------------------
    # Find the min/max timestamp among records with a valid dt.
    valid_dts = [r["dt"] for r in parsed if r["dt"] is not None]
    windows = []

    if valid_dts:
        min_dt = min(valid_dts)
        max_dt = max(valid_dts)
        delta = timedelta(days=window_days)

        # Align window start to UTC midnight of the day containing min_dt.
        window_start = min_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        while window_start <= max_dt:
            window_end = window_start + delta
            bucket = [
                r for r in parsed
                if r["dt"] is not None and window_start <= r["dt"] < window_end
            ]
            if bucket:
                b_adopted = sum(1 for r in bucket if r["adopted"] is True)
                b_total = len(bucket)
                b_rate = (b_adopted / b_total) if b_total > 0 else 0.0
                b_dists = [r["edit_dist"] for r in bucket if r["edit_dist"] is not None]
                b_avg_dist = (sum(b_dists) / len(b_dists)) if b_dists else None
                windows.append({
                    "start": window_start.strftime("%Y-%m-%d"),
                    "end": (window_end - timedelta(days=1)).strftime("%Y-%m-%d"),
                    "total": b_total,
                    "adopted": b_adopted,
                    "adoption_rate": b_rate,
                    "avg_edit_dist_phash": b_avg_dist,
                })
            window_start = window_end

    # ---- Tag frequency -------------------------------------------------
    adopted_tag_freq = defaultdict(lambda: defaultdict(int))
    rejected_tag_freq = defaultdict(lambda: defaultdict(int))

    for r in parsed:
        tags = r["tags"]
        if not isinstance(tags, dict) or not tags:
            continue
        dest = None
        if r["adopted"] is True:
            dest = adopted_tag_freq
        elif r["adopted"] is False:
            dest = rejected_tag_freq
        if dest is None:
            continue
        for key, value in tags.items():
            if isinstance(value, str):
                dest[key][value] += 1
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, str):
                        dest[key][v] += 1

    # Convert defaultdicts to plain dicts for JSON serialisation.
    top_adopted_tags = {k: dict(v) for k, v in adopted_tag_freq.items()}
    top_rejected_tags = {k: dict(v) for k, v in rejected_tag_freq.items()}

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_records": total_records,
        "overall": {
            "adoption_rate": adoption_rate,
            "avg_edit_dist_phash": avg_edit_dist,
            "adopted_count": adopted_count,
            "rejected_count": rejected_count,
        },
        "windows": windows,
        "top_adopted_tags": top_adopted_tags,
        "top_rejected_tags": top_rejected_tags,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    home = os.environ.get("HOME", "/tmp")
    default_db = str(Path(home) / ".vrm-pipeline" / "ledger.db")

    parser = argparse.ArgumentParser(
        description="eval-B: adoption rate and edit-distance time-series aggregation"
    )
    parser.add_argument(
        "--db-path",
        default=default_db,
        help=f"Path to ledger SQLite DB (default: {default_db})",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Window size in days for time-series grouping (default: 7)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path (default: stdout)",
    )

    args = parser.parse_args()

    rows = load_records(args.db_path)
    report = build_report(rows, args.window_days)
    output_str = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_str, encoding="utf-8")
        print(f"[eval_b] report written to {out_path}", file=sys.stderr)
    else:
        print(output_str)


if __name__ == "__main__":
    main()
