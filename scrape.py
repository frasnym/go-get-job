#!/usr/bin/env python3
"""
linkedin-jobs-daily — scrape LinkedIn's public (no-login) guest jobs endpoint.

Walks keywords x locations from config.toml with LinkedIn's native 24h filter
(f_TPR), stores results in SQLite with dedupe, and optionally pulls the full
job description from each job's detail page.

Stdlib only. Python 3.11+ (tomllib).

    python3 scrape.py [--config config.toml] [--no-details] [-v]

Exit codes: 0 = every query succeeded, 1 = at least one query failed
(so cron can tell the difference between "no jobs" and "scrape broke").
"""

from __future__ import annotations

import argparse
import html.parser
import json
import random
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# f_TPR values LinkedIn accepts (seconds -> token)
F_TPR = {1: "r86400", 7: "r604800", 30: "r2592000"}

NOW = datetime.now(timezone.utc)


def log(msg: str, verbose: bool = True) -> None:
    if verbose:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #

class _CardParser(html.parser.HTMLParser):
    """Extracts job cards from the list endpoint's <li> response."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jobs: list[dict] = []
        self.cur: dict | None = None
        self.cap: tuple[str, str, int, list[str]] | None = None  # key, tag, depth, buf

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        urn = a.get("data-entity-urn") or ""
        if tag == "div" and "urn:li:jobPosting:" in urn:
            self.cur = {"job_id": urn.rsplit(":", 1)[-1]}
        if self.cur is None:
            return
        if tag == "a" and "base-card__full-link" in cls and "url" not in self.cur:
            self.cur["url"] = (a.get("href") or "").split("?")[0]
        elif self.cap is None and tag == "h3" and "base-search-card__title" in cls:
            self.cap = ("title", tag, 1, [])
        elif self.cap is None and tag == "h4" and "base-search-card__subtitle" in cls:
            self.cap = ("company", tag, 1, [])
        elif self.cap is None and tag == "span" and "job-search-card__location" in cls:
            self.cap = ("job_location", tag, 1, [])
        elif self.cap is None and tag == "time":
            self.cur.setdefault("posted_date", a.get("datetime") or "")
            self.cap = ("posted_text", tag, 1, [])
        elif self.cap is not None and tag == self.cap[1]:
            self.cap = (self.cap[0], tag, self.cap[2] + 1, self.cap[3])

    def handle_endtag(self, tag):
        if self.cap and tag == self.cap[1]:
            key, _, depth, buf = self.cap
            if depth == 1:
                if self.cur is not None:
                    self.cur[key] = " ".join("".join(buf).split())
                self.cap = None
            else:
                self.cap = (key, tag, depth - 1, buf)
        elif tag == "li" and self.cur is not None:
            if self.cap is not None:  # unclosed capture inside a broken card
                self.cap = None
            if self.cur.get("job_id"):
                self.jobs.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.cap is not None:
            self.cap[3].append(data)


class _DetailParser(html.parser.HTMLParser):
    """Extracts description, criteria and location from a job detail page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description = ""
        self.criteria: dict[str, str] = {}
        self.location = ""
        self.posted_text = ""
        self.cap: tuple[str, str, int, list[str]] | None = None
        self.in_criteria = False
        self._crit_label: str | None = None

    @staticmethod
    def _is_location_bullet(cls: str) -> bool:
        """LinkedIn reuses topcard__flavor--bullet for both the job location
        and the applicant count (num-applicants__caption class) — only the
        location one counts, and only the first one we see."""
        return (
            "topcard__flavor--bullet" in cls
            and "num-applicants" not in cls
            and "topcard__flavor--metadata" not in cls
        )

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "li" and "description__job-criteria-item" in cls:
            self.in_criteria = True
            self._crit_label = None
            return
        if self.cap is None:
            if tag == "div" and "description__text" in cls:
                self.cap = ("description", tag, 1, [])
            elif tag == "span" and "posted-time-ago__text" in cls:
                self.cap = ("posted_text", tag, 1, [])
            elif tag == "span" and self._is_location_bullet(cls):
                self.cap = ("location", tag, 1, [])
            elif self.in_criteria and tag == "h3":
                self.cap = ("crit_label", tag, 1, [])
            elif self.in_criteria and "description__job-criteria-text" in cls:
                self.cap = ("crit_value", tag, 1, [])
        elif tag == self.cap[1]:
            self.cap = (self.cap[0], tag, self.cap[2] + 1, self.cap[3])

    def handle_endtag(self, tag):
        if self.cap and tag == self.cap[1]:
            key, _, depth, buf = self.cap
            if depth == 1:
                text = " ".join("".join(buf).split())
                if key == "description":
                    self.description = text
                elif key == "posted_text":
                    self.posted_text = text
                elif key == "location" and not self.location:
                    self.location = text  # first valid bullet wins
                elif key == "crit_label":
                    self._crit_label = text
                elif key == "crit_value" and self._crit_label:
                    self.criteria[self._crit_label] = text
                self.cap = None
            else:
                self.cap = (key, tag, depth - 1, buf)
        elif tag == "li" and self.in_criteria:
            self.in_criteria = False
            self._crit_label = None

    def handle_data(self, data):
        if self.cap is not None:
            self.cap[3].append(data)


# --------------------------------------------------------------------------- #
# Time parsing — "12 hours ago" / "3 days ago" -> age in hours (None = unknown)
# --------------------------------------------------------------------------- #

_AGE_RE = re.compile(r"(\d+)\s*(minute|hour|day|week)s?\s+ago", re.I)
_MULT = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 24 * 7}


def age_hours(posted_text: str, posted_date: str) -> float | None:
    """Best-effort age of a job posting in hours. None = could not determine."""
    m = _AGE_RE.search(posted_text or "")
    if m:
        return int(m.group(1)) * _MULT[m.group(2).lower()]
    if re.search(r"just posted|today|\bfew minutes\b", posted_text or "", re.I):
        return 0.0
    if posted_date:
        try:
            d = datetime.fromisoformat(posted_date[:10]).replace(tzinfo=timezone.utc)
            return (NOW - d).total_seconds() / 3600
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_get(url: str, ua: str, timeout: int, logf=log, verbose: bool = True):
    """GET with retry/backoff on rate limiting.

    Returns (status, body) where status is an int HTTP code, or 0 on network
    failure after retries. 429/999/5xx are treated as transient.
    """
    last_err = ""
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 999) or 500 <= e.code < 600:
                wait = (5, 20, 60)[min(attempt, 2)]
                logf(f"HTTP {e.code} from {url.split('?')[0]} — backing off {wait}s", verbose)
                time.sleep(wait)
                last_err = f"HTTP {e.code}"
                continue
            return e.code, ""  # 403/404 etc: not transient, give up
        except Exception as e:  # timeout, DNS, connection reset
            wait = (5, 20, 60)[min(attempt, 2)]
            logf(f"{type(e).__name__} on {url.split('?')[0]} — retrying in {wait}s", verbose)
            time.sleep(wait)
            last_err = type(e).__name__
    logf(f"giving up on {url.split('?')[0]} ({last_err})", verbose)
    return 0, ""


def pace(delay: float, jitter: float) -> None:
    time.sleep(delay + (random.uniform(0, jitter) if jitter > 0 else 0))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id        TEXT PRIMARY KEY,
  url           TEXT,
  title         TEXT,
  company       TEXT,
  job_location  TEXT,
  posted_date   TEXT,
  posted_text   TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  detail_fetched INTEGER NOT NULL DEFAULT 0,
  description   TEXT,
  criteria_json TEXT
);
CREATE TABLE IF NOT EXISTS job_queries (
  job_id     TEXT NOT NULL,
  keyword    TEXT NOT NULL,
  location   TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (job_id, keyword, location)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  queries_ok     INTEGER DEFAULT 0,
  queries_failed INTEGER DEFAULT 0,
  jobs_seen      INTEGER DEFAULT 0,
  new_jobs       INTEGER DEFAULT 0,
  http_errors    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
"""


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def upsert_job(con: sqlite3.Connection, job: dict) -> bool:
    """Insert-or-update one job. Returns True if it was new to the database."""
    now = NOW.isoformat(timespec="seconds")
    cur = con.execute(
        """INSERT OR IGNORE INTO jobs
           (job_id, url, title, company, job_location, posted_date, posted_text,
            first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            job["job_id"],
            job.get("url", ""),
            job.get("title", ""),
            job.get("company", ""),
            job.get("job_location", ""),
            job.get("posted_date", ""),
            job.get("posted_text", ""),
            now,
            now,
        ),
    )
    is_new = cur.rowcount == 1
    if not is_new:
        con.execute("UPDATE jobs SET last_seen=? WHERE job_id=?", (now, job["job_id"]))
    con.execute(
        """INSERT OR IGNORE INTO job_queries (job_id, keyword, location, first_seen)
           VALUES (?,?,?,?)""",
        (job["job_id"], job["keyword"], job["location"], now),
    )
    return is_new


# --------------------------------------------------------------------------- #
# Main scrape loop
# --------------------------------------------------------------------------- #

def scrape(cfg: dict, use_details: bool, verbose: bool = True) -> int:
    sc = cfg["scraper"]
    keywords = sc["keywords"]
    locations = sc["locations"]
    days = int(sc.get("days_back", 1))
    max_pages = int(sc.get("max_pages", 4))
    delay = float(sc.get("delay_sec", 1.5))
    jitter = float(sc.get("jitter_sec", 0.75))
    ua = sc["user_agent"]
    timeout = int(sc.get("timeout_sec", 25))
    tpr = F_TPR.get(days, "r86400")
    window_h = days * 24

    det = cfg.get("details", {})
    do_details = use_details and bool(det.get("fetch", True))
    detail_cap = int(det.get("max_per_run", 0))

    db_path = cfg["storage"]["database"]
    con = open_db(db_path)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = con.execute(
        "INSERT INTO runs (started_at) VALUES (?)", (started,)
    ).lastrowid

    http_errors = 0
    queries_ok = 0
    queries_failed = 0
    jobs_seen = 0
    new_jobs = 0

    log(f"config: {len(keywords)} keywords x {len(locations)} locations, "
        f"window={window_h}h, details={'on' if do_details else 'off'}", verbose)

    for loc in locations:
        for kw in keywords:
            seen_ids: set[str] = set()
            got = 0
            for page in range(max_pages):
                start = page * 10
                q = urllib.parse.urlencode(
                    {"keywords": kw, "location": loc, "f_TPR": tpr, "start": start}
                )
                status, body = http_get(f"{LIST_URL}?{q}", ua, timeout, verbose=verbose)
                if status == 200 and body:
                    parser = _CardParser()
                    parser.feed(body)
                    cards = parser.jobs
                else:
                    cards = []
                    http_errors += 1
                if status != 200:
                    queries_failed += 1
                    log(f"FAIL '{kw}' @ {loc} page {page} (HTTP {status})", verbose)
                    break
                if not cards:
                    break  # LinkedIn exhausted this query

                page_new = 0
                for card in cards:
                    jid = card["job_id"]
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    card["keyword"], card["location"] = kw, loc
                    age = age_hours(card.get("posted_text", ""), card.get("posted_date", ""))
                    if age is not None and age > window_h + 6:
                        continue  # server-side filter leaked an older row
                    jobs_seen += 1
                    if upsert_job(con, card):
                        new_jobs += 1
                        page_new += 1
                    got += 1
                if page_new == 0 and page > 0:
                    # nothing new deeper in the pagination — but keep going only
                    # if the page had results (job could be re-shown under a query)
                    pass
                if len(cards) < 10:
                    break  # short page = end of results
                pace(delay, jitter)
            else:
                pass

            queries_ok += 1
            log(f"ok   '{kw}' @ {loc}: {got} jobs in window "
                f"({len(seen_ids)} raw)", verbose)
            pace(delay, jitter)

    # ---- optional detail pass (new + capped) --------------------------------
    if do_details:
        pending = con.execute(
            """SELECT job_id FROM jobs WHERE detail_fetched = 0
               ORDER BY first_seen DESC LIMIT ?""",
            (detail_cap if detail_cap > 0 else -1,),
        ).fetchall()
        if pending:
            log(f"fetching details for {len(pending)} jobs", verbose)
        for (jid,) in pending:
            status, body = http_get(DETAIL_URL.format(job_id=jid), ua, timeout,
                                    verbose=verbose)
            if status != 200:
                http_errors += 1
                pace(delay, jitter)
                continue
            p = _DetailParser()
            p.feed(body)
            con.execute(
                """UPDATE jobs SET description=?, criteria_json=?,
                       job_location=COALESCE(?, job_location),
                       detail_fetched=1 WHERE job_id=?""",
                (
                    p.description or None,
                    json.dumps(p.criteria, ensure_ascii=False) if p.criteria else None,
                    p.location or None,
                    jid,
                ),
            )
            pace(delay, jitter)

    con.execute(
        """UPDATE runs SET finished_at=?, queries_ok=?, queries_failed=?,
               jobs_seen=?, new_jobs=?, http_errors=? WHERE run_id=?""",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            queries_ok,
            queries_failed,
            jobs_seen,
            new_jobs,
            http_errors,
            run_id,
        ),
    )
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    con.close()

    log(f"done: {jobs_seen} matched window, {new_jobs} new, "
        f"{total} total in {db_path}; queries ok={queries_ok} "
        f"failed={queries_failed} http_errors={http_errors}", verbose)

    # distinct failure → non-zero so cron can alert
    return 0 if queries_failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape LinkedIn guest jobs into SQLite")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--no-details", action="store_true",
                    help="skip detail-page fetches even if enabled in config")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path} (expected next to this script)",
              file=sys.stderr)
        return 2
    with cfg_path.open("rb") as fh:
        cfg = tomllib.load(fh)

    return scrape(cfg, use_details=not args.no_details, verbose=not args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
