# scry

Dorking + contact harvesting in one tool. Built for pentesters who got tired of running three different scripts and copy-pasting between them.

**`contacts`** grabs employee names from Google (LinkedIn, RocketReach, ZoomInfo) and spits out email lists.
**`files`** dorks for sensitive files on a target domain and downloads them.

Works with Serper API, a headless browser, or both at once.

## Setup

```bash
cd scry
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Grab a free Serper key (2,500 queries, no card): [serper.dev](https://serper.dev)

## How it works

### Serper mode (`--source serper`)

Hits the Serper API directly. Fast, no browser, no CAPTCHA. Each query paginates up to 100 pages (1000 results). Uses one API credit per page.

### Browser mode (`--source browser`)

Opens a real Chromium window via Playwright and scrapes Google like a human would. No API key needed, but Google will eventually throw a CAPTCHA at you.

When that happens, scry pauses and tells you:

```
╭──────────── Action Required ────────────╮
│ CAPTCHA detected! Solve it in the       │
│ browser, then press ENTER here.         │
╰─────────────────────────────────────────╯
```

Just switch to the Chromium window, click the "I'm not a robot" checkbox (or solve the puzzle), then come back to your terminal and hit Enter. scry picks up right where it left off.

If you close the browser window by accident, scry notices, stops gracefully, and keeps whatever it already gathered.

### Auto mode (default)

If you pass `--api-key`, scry runs Serper first (fast, bulk results) then follows up with browser scraping to catch anything the API missed. Results from both are merged and deduplicated.

No API key? It just runs browser mode and gives you a nudge about the free Serper tier.

## Usage

All flags go **after** the subcommand. Think `git commit -m "msg"`, not `git -m "msg" commit`.

### Contacts

```bash
# Just works (browser mode)
python scry.py contacts -c "Acme Inc" -d acme.com

# With API (way faster)
python scry.py contacts -c "Acme Inc" -d acme.com --api-key KEY

# API only, 50 pages deep, email format 3 (flast), save names too
python scry.py contacts -c "Acme Inc" -d acme.com \
    --api-key KEY --source serper -p 50 -f 3 --save-names names.txt

# Structured output directory (emails.txt + emails.json + names.txt + run.log)
python scry.py contacts -c "Acme Inc" -d acme.com \
    --api-key KEY --output-dir output

# JSON to stdout, pipe wherever
python scry.py contacts -c "Acme Inc" -d acme.com \
    --api-key KEY --format-output json --stdout | jq '.[] | .email'
```

**Required:** `-c` (company name) and `-d` (domain for email generation).

**Email formats** (`-f N`):

| # | Pattern | Example |
|---|---------|---------|
| 1 | first.last | john.doe@acme.com |
| 2 | firstlast | johndoe@acme.com |
| 3 | flast | jdoe@acme.com |
| 4 | first | john@acme.com |
| 5 | last | doe@acme.com |
| 6 | last.first | doe.john@acme.com |
| 7 | first_last | john_doe@acme.com |
| 8 | f.last | j.doe@acme.com |
| 9 | firstl | johnd@acme.com |
| 10 | first.last1 | john.doe1@acme.com |

### Files

```bash
# Single dork
python scry.py files -d acme.com \
    -q "site:{domain} filetype:pdf" --api-key KEY

# Full sweep with 72 built-in dorks + download everything
python scry.py files -d acme.com -c "Acme" \
    --dorks-file dorks.txt --download --output-dir output --api-key KEY

# Go deep: 100 pages per dork, Serper only
python scry.py files -d acme.com --dorks-file dorks.txt \
    --source serper --api-key KEY -p 100

# Already have a URL list? Skip search, just download
python scry.py files --input-file links.txt --download

# Download through a proxy
python scry.py files --input-file links.txt \
    --download --proxy http://127.0.0.1:8080
```

**Placeholders:** Use `{domain}` and `{company}` in dorks. They get swapped for your `-d` and `-c` values:

```
site:{domain} filetype:pdf  -->  site:acme.com filetype:pdf
```

**dorks.txt** ships with 72 dorks across 11 categories (sensitive docs, configs, credentials, DB dumps, admin panels, open dirs, error pages, API keys, cloud storage, source code, infra discovery). Just point it at a target and go.

## Output

Without `--output-dir`, results go to a single file (`emails.txt` or `file_links.txt`).

With `--output-dir`, each run gets its own timestamped folder:

```
output/
  2026-02-25_143022_contacts_Acme/
    emails.txt        emails.json       names.txt
    raw_titles.txt    run.log

  2026-02-25_150512_files_acme_com/
    file_links.txt    file_links.json   run.log
    downloads/
```

**Formats** (`--format-output txt|json|csv`):
- **txt** -- one item per line, copy-paste ready
- **json** -- full metadata (source, raw title, which dork found it)
- **csv** -- same fields, spreadsheet-friendly

Use `--stdout` to pipe output instead of writing files.

## Ctrl+C

Hit Ctrl+C anytime. scry asks you:

```
s = skip to next  |  q = quit (save what we have)
```

Pick `s` to skip the current dork and move on. Pick `q` to stop and save everything gathered so far. Either way, nothing is lost.

## Other flags

| Flag | What it does |
|------|-------------|
| `--dry-run` | Show what would run, don't actually do it |
| `--quiet` | Errors only, no progress output |
| `--verbose` | Extra detail for debugging |
| `--flaresolverr URL` | Route downloads through FlareSolverr for Cloudflare |
| `--no-resume` | Re-download files that already exist locally |
| `--show-examples` | Print a table of example dorks (the only flag that goes before the subcommand) |

## Config file

Save your defaults in `~/.scry.yaml` so you don't have to type them every time:

```yaml
api_key: your_serper_key
pages: 10
delay: 3
output_dir: output
```

CLI flags override config. You can also set `SERPER_API_KEY` as an env variable.
