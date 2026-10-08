# linkedin-jobs-daily

Daily LinkedIn job scraping for a fixed set of keywords and locations, with a
native **last-24-hours** filter, SQLite dedupe, and a daily digest that only
shows jobs you haven't seen before.

Runs against LinkedIn's **public guest jobs endpoint** — no login, no cookies,
no API keys, no third-party platform. Python standard library only.

## Requirements

- Python **3.11+** (uses `tomllib` from the standard library)
- Nothing else — no `pip install`

## Quick start

```bash
python3 scrape.py            # scrape keywords x locations into jobs.db
python3 digest.py            # print jobs found in the last 24h (markdown)
python3 digest.py --out digests/today.md   # write it to a file instead
```

Edit `config.toml` to change keywords, locations, pacing, or the time window.

## Daily cron

One line in `crontab -e` — scrape at 08:00, then render the digest:

```cron
0 8 * * *  cd /path/to/linkedin-jobs-daily && python3 scrape.py -q && python3 digest.py --out digests/$(date +\%F).md
```

`scrape.py` exits `1` if any query failed, so a wrapper script can alert on
failure instead of silently logging "0 jobs".

## Choosing a schedule

**Recommended: one run, all locations, once a day.** Every job stays visible
for the whole 24h window, so a single daily pass collects everything — and
the digest shows each job once regardless of how many runs fetched it.

Why not hourly:

- The window is 24h, so hourly runs collect the same jobs a daily run does.
- Dedupe keys on `first_seen`: extra runs within the window don't produce
  extra digest entries, they just collect the same set in pieces.
- The ~40 results-per-query cap is **per window, not per run** — 24 hourly
  runs hit the same cap instead of stacking into more results.

Why not one location per run: same total requests as running them together
(`keywords x locations` either way), but more schedules to maintain, and
dedupe already merges jobs that appear under several locations or keywords.

**Compromise if you want fresher coverage:** 2–4 runs a day, still all
locations at once. Shown here at 3 runs (08:00 / 14:00 / 20:00) with
`--since 7` so each digest covers the gap since the previous run without
re-listing yesterday's jobs:

```cron
0 8,14,20 * * *  cd /path/to/linkedin-jobs-daily && python3 scrape.py -q && python3 digest.py --since 7 --out digests/$(date +\%F)-$(date +\%H).md
```

Hourly only makes sense when a query floods past the ~40 cap — even then
LinkedIn orders results by relevance, not date, so hourly sampling catches
extra jobs only partially. And as queries are added, consider raising
`delay_sec` to `2.5`: more queries per run means crossing the rate-limit
line that testing already hit at `1.5` around request ~40.

## Configuration (`config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `scraper.keywords` | 4 sample keywords | every keyword is queried |
| `scraper.locations` | `Ireland` | every location is queried |
| `scraper.days_back` | `1` | time window: `1` = last 24h (`f_TPR=r86400`) |
| `scraper.max_pages` | `4` | pages per query, 10 jobs per page (LinkedIn caps ~40/query) |
| `scraper.delay_sec` / `jitter_sec` | `1.5` / `0.75` | pacing between requests |
| `details.fetch` | `true` | pull full description + criteria per job (1 request each) |
| `details.max_per_run` | `50` | cap on detail fetches per run (`0` = no cap) |
| `storage.database` | `jobs.db` | SQLite file |

Total requests per run ≈ `keywords × locations × pages` **+** new jobs
(details on). With the shipped config: 4 × 1 × 4 ≈ 16 listing requests, plus
up to 50 detail requests on a fresh database — a couple of minutes at default
pacing.

## How dedupe works

- `jobs.job_id` is the primary key (`urn:li:jobPosting:<id>`), so a job found
  again by another query is updated, not duplicated.
- `jobs.first_seen` records when it first entered the database; `digest.py`
  only shows rows with `first_seen` inside your window, so day 2 never
  re-lists day 1's jobs even though both runs fetch a 24h window.
- `job_queries` keeps which keyword/location matched each job.

## Database schema

```
jobs(job_id PK, url, title, company, job_location, posted_date, posted_text,
     first_seen, last_seen, detail_fetched, description, criteria_json)
job_queries(job_id, keyword, location, first_seen, PK(job_id, keyword, location))
runs(run_id PK, started_at, finished_at, queries_ok, queries_failed,
     jobs_seen, new_jobs, http_errors)
```

`runs` gives you a one-row-per-run history you can alert on.

## Limits worth knowing

- **~40 results per keyword+location/day** — LinkedIn's own pagination cap,
  not this script's. A flooded location needs the keyword split into city
  queries.
- **`f_TPR` is a server-side hint**, so a few rows land slightly outside the
  window; the script double-checks the "N hours ago" stamp and drops anything
  older than the window + 6h slack.
- **Rate limits:** pacing plus backoff on HTTP 429/999 (LinkedIn's block code).
  If this box's IP starts getting blocked, raise `delay_sec` before anything
  else. The script never uses an account, so there is nothing to ban but the IP.
- **robots.txt lists `/jobs-guest/` as disallowed.** Scraping public job
  listings isn't a CFAA issue in the US (*hiQ v. LinkedIn*), but it does
  contradict LinkedIn's User Agreement — a contract matter between you and
  LinkedIn. Your call; the code makes no attempt to hide what it is.

## Tested

Verified against the live endpoint on **2026-10-08**: listing endpoint
(200s across pagination), `f_TPR=r86400` window filtering, detail pages
(description + criteria), SQLite upsert dedupe (second run = 0 new), and
digest rendering. Two HTTP 429s were hit during testing at
`delay_sec=1.5`; the backoff recovered both without data loss — if your IP
sees them often, raise the delay. Not tested: your production cron host, and
LinkedIn's behavior if this runs from the same IP for months — expect to tune
`delay_sec` eventually.
