#!/usr/bin/env python3
"""
linkedin-jobs-daily — render a digest of newly-found jobs from jobs.db.

Shows only jobs that entered the database within the last N hours (default 24),
so consecutive daily runs never repeat yesterday's listings.

    python3 digest.py                     # markdown to stdout, last 24h
    python3 digest.py --since 24 --out digests/today.md
    python3 digest.py --json              # machine-readable

Exit codes: 0 = digest produced, 1 = no new jobs in the window,
2 = database/config missing (nothing was silently "empty").
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_db_path(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    with config_path.open("rb") as fh:
        cfg = tomllib.load(fh)
    return cfg.get("storage", {}).get("database", "jobs.db")


def collect(db_path: str, since_hours: float) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(
        timespec="seconds"
    )
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT j.job_id, j.title, j.company, j.job_location, j.url,
                  j.posted_text, j.posted_date, j.first_seen, j.description,
                  j.criteria_json,
                  GROUP_CONCAT(q.keyword, char(10)) AS keywords,
                  GROUP_CONCAT(q.location, char(10))  AS qlabels
           FROM jobs j
           JOIN job_queries q ON q.job_id = j.job_id
           WHERE j.first_seen >= ?
           GROUP BY j.job_id
           ORDER BY j.first_seen DESC, j.title""",
        (cutoff,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def group(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        kws = (r["keywords"] or "").split("\n")
        locs = (r["qlabels"] or "").split("\n")
        # group under the query that found it; a job found by several keywords
        # is listed under its first keyword only (keeps the digest deduped)
        grouped[(kws[0], locs[0])].append(r)
    return dict(grouped)


def render_markdown(rows: list[dict], since_hours: float) -> str:
    now = datetime.now(timezone.utc)
    when = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# LinkedIn jobs — {when}",
        "",
        f"New in the last {since_hours:g}h: **{len(rows)}** jobs",
        "",
    ]
    grouped = group(rows)
    for (kw, loc), items in sorted(grouped.items()):
        lines.append(f"## {kw} @ {loc} ({len(items)})")
        lines.append("")
        for r in items:
            title = r["title"] or "(untitled)"
            company = r["company"] or "—"
            job_loc = r["job_location"] or ""
            posted = r["posted_text"] or r["posted_date"] or ""
            head = f"- **{title}** — {company}"
            if job_loc and job_loc not in company:
                head += f" · {job_loc}"
            lines.append(head)
            detail = [p for p in (posted,) if p]
            if r["url"]:
                detail.append(f"[apply]({r['url']})")
            if detail:
                lines.append(f"  {' · '.join(detail)}")
            # one-line description teaser if we fetched the detail page
            if r["description"]:
                teaser = r["description"][:220].strip()
                if len(r["description"]) > 220:
                    teaser += "…"
                lines.append(f"  > {teaser}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Digest of newly scraped LinkedIn jobs")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--since", type=float, default=24,
                    help="hours to look back (default: 24)")
    ap.add_argument("--out", default=None,
                    help="write markdown here instead of stdout "
                         "(directory comes from config [digest].output_dir)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    db_path = load_db_path(cfg_path)
    if db_path is None:
        print(f"config not found: {cfg_path}", file=sys.stderr)
        return 2
    if not Path(db_path).exists():
        print(f"database not found: {db_path} — run scrape.py first", file=sys.stderr)
        return 2

    rows = collect(db_path, args.since)
    if args.json:
        payload = {
            "since_hours": args.since,
            "count": len(rows),
            "jobs": [
                {k: r[k] for k in ("title", "company", "job_location", "url",
                                   "posted_text", "first_seen")}
                for r in rows
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        text = render_markdown(rows, args.since)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"{len(rows)} jobs -> {out}")
    else:
        print(text)

    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
