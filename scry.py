#!/usr/bin/env python3
"""
scry - OSINT & dorking toolkit
contacts: names/emails | files: dork + download
"""

import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

import requests
from playwright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERPER_ENDPOINT = "https://google.serper.dev/search"
SERPER_MAX_PAGES = 100

NO_API_KEY_MSG = (
    "[yellow]No Serper API key provided. Running browser-only mode.[/yellow]\n"
    "Serper.dev gives 2,500 free queries — skip the CAPTCHA roulette.\n"
    "Grab a key at [link=https://serper.dev]https://serper.dev[/link]"
)

CONTACT_QUERIES = [
    'site:linkedin.com/in/ "{company}"',
    'site:rocketreach.co "{domain}"',
    'site:zoominfo.com/p/ "{company}"',
]

EMAIL_FORMATS = {
    1: lambda f, l, d: f"{f}.{l}@{d}",
    2: lambda f, l, d: f"{f}{l}@{d}",
    3: lambda f, l, d: f"{f[0]}{l}@{d}",
    4: lambda f, l, d: f"{f}@{d}",
    5: lambda f, l, d: f"{l}@{d}",
    6: lambda f, l, d: f"{l}.{f}@{d}",
    7: lambda f, l, d: f"{f}_{l}@{d}",
    8: lambda f, l, d: f"{f[0]}.{l}@{d}",
    9: lambda f, l, d: f"{f}{l[0]}@{d}",
    10: lambda f, l, d: f"{f}.{l}1@{d}",
}

EMAIL_FORMAT_HELP = (
    "1=first.last  2=firstlast  3=flast  4=first  5=last  "
    "6=last.first  7=first_last  8=f.last  9=firstl  10=first.last1"
)

DORK_EXAMPLES = [
    ("site:{domain} filetype:pdf", "PDFs on target domain"),
    ("site:{domain} filetype:doc OR filetype:docx", "Word docs"),
    ("site:{domain} filetype:xlsx OR filetype:xls", "Spreadsheets"),
    ("site:{domain} filetype:pptx OR filetype:ppt", "Presentations"),
    ('inurl:admin site:{domain}', "Admin panels"),
    ('intitle:"index of" site:{domain}', "Open directories"),
    ('site:{domain} inurl:login', "Login pages"),
    ('site:{domain} filetype:env OR filetype:cfg', "Config files"),
    ('site:linkedin.com/in/ "{company}"', "LinkedIn profiles"),
    ('site:rocketreach.co "{domain}"', "RocketReach contacts"),
]

MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
}

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

TITLE_NOISE = {
    "dr", "mr", "mrs", "ms", "prof", "sir", "phd", "cpa", "cfa",
    "the", "and", "for", "with", "from", "about", "into",
    "top", "best", "new", "old", "bad", "good", "big", "open",
    "all", "any", "how", "why", "who", "what", "our", "you",
    "security", "cyber", "cloud", "data", "team", "lead",
    "senior", "junior", "staff", "chief", "head", "vice",
    "mad", "pro", "iii", "inc", "llc", "ltd",
}

console = Console()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def resolve_placeholders(text: str, domain: str | None, company: str | None) -> str:
    if "{domain}" in text and not domain:
        raise ValueError("Dork contains {domain} but --domain not provided")
    if "{company}" in text and not company:
        raise ValueError("Dork contains {company} but --company not provided")
    out = text
    if domain:
        out = out.replace("{domain}", domain)
    if company:
        out = out.replace("{company}", company)
    return out


def is_file_link(url: str) -> bool:
    parsed = urlparse(url.lower())
    path = parsed.path
    if not path or path.endswith("/"):
        return False
    if re.search(r"\.[a-z0-9]{2,5}$", path):
        return True
    if re.search(r"\.[a-z0-9]{2,5}[?#]", url.lower()):
        return True
    return False


def clean_google_url(url: str) -> str | None:
    if "/url?q=" in url or "/url?url=" in url:
        m = re.search(r"[?&](?:q|url)=([^&]+)", url)
        if m:
            return unquote(m.group(1))
    if url.startswith("http"):
        return url
    return None


def sanitize_filename(name: str) -> str:
    for c in '<>:"|?*':
        name = name.replace(c, "_")
    name = name.replace("/", "_").replace("\\", "_")
    name = "".join(c for c in name if ord(c) >= 32)
    name = name.strip(". ")
    if not name:
        name = "file"
    if os.path.splitext(name)[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name


def format_size(n: float) -> str:
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def load_config(path: str | None) -> dict:
    if not yaml:
        return {}
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.expanduser("~/.scry.yaml"))
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
    return {}


def _ask_continue() -> bool:
    """On Ctrl+C: ask user to skip current operation or exit entirely."""
    try:
        console.print("\n[yellow]Ctrl+C pressed. Skip current operation or exit?[/yellow]")
        console.print("  [bold]s[/bold] = skip to next  |  [bold]q[/bold] = quit (save what we have)")
        choice = input("  > ").strip().lower()
        return choice != "q"
    except (KeyboardInterrupt, EOFError):
        return False


def _source_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def make_run_dir(base: str, kind: str, label: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40]
    d = os.path.join(base, f"{ts}_{kind}_{safe_label}")
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def write_run_log(run_dir: str, lines: list[str]) -> None:
    with open(os.path.join(run_dir, "run.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Serper API
# ---------------------------------------------------------------------------

def serper_search(query: str, api_key: str, page: int = 1) -> dict | None:
    try:
        payload = {"q": query, "num": 10}
        if page > 1:
            payload["page"] = page
        r = requests.post(
            SERPER_ENDPOINT,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code == 401:
            console.print("[red]API key invalid. Get one at https://serper.dev[/red]")
            return None
        if r.status_code == 429:
            console.print("[yellow]Rate limited by Serper. Wait and retry.[/yellow]")
            return None
        if r.status_code == 400:
            console.print(f"[red]Serper 400: {r.text[:200]}[/red]")
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        console.print(f"[red]Serper error: {e}[/red]")
        return None


def serper_fetch_organic(query: str, api_key: str, max_pages: int) -> list[dict]:
    all_results = []
    for p in range(1, min(max_pages, SERPER_MAX_PAGES) + 1):
        data = serper_search(query, api_key, p)
        if not data:
            break
        organic = data.get("organic") or []
        if not organic:
            break
        all_results.extend(organic)
    return all_results


def serper_fetch_file_links(query: str, api_key: str, max_pages: int) -> list[tuple[str, str]]:
    """Returns list of (url, dork) tuples."""
    results = []
    seen = set()
    for p in range(1, min(max_pages, SERPER_MAX_PAGES) + 1):
        data = serper_search(query, api_key, p)
        if not data:
            break
        organic = data.get("organic") or []
        if not organic:
            break
        for r in organic:
            href = r.get("link") or r.get("url", "")
            if href and is_file_link(href) and href not in seen:
                seen.add(href)
                results.append((href, query))
            for sl in r.get("sitelinks") or []:
                u = sl.get("link") or sl.get("url", "")
                if u and is_file_link(u) and u not in seen:
                    seen.add(u)
                    results.append((u, query))
    return results


# ---------------------------------------------------------------------------
# Playwright Browser
# ---------------------------------------------------------------------------

def _is_browser_closed(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "has been closed" in msg or "target closed" in msg or "connection closed" in msg


async def _handle_consent_and_captcha(page, quiet: bool) -> None:
    for sel in ['button:has-text("Accept all")', 'button:has-text("Reject all")', "#L2AGLb", 'button[id="W0wltc"]']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                if not quiet:
                    console.print("  [dim]Handling cookie consent...[/dim]")
                await btn.click()
                await page.wait_for_timeout(1000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(2000)
    html = await page.content()
    if "recaptcha" in html.lower() or "captcha" in html.lower():
        console.print(Panel(
            "CAPTCHA detected! Solve it in the browser, then press ENTER here.",
            title="[yellow]Action Required[/yellow]", border_style="yellow",
        ))
        input()
        await page.wait_for_timeout(2000)


async def playwright_fetch_titles(queries: list[str], max_pages: int, delay: int, quiet: bool, partial_out: list | None = None) -> list[tuple[str, str]]:
    results = partial_out if partial_out is not None else []
    browser_dead = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
                viewport={"width": 1600, "height": 900}, locale="en-US",
            )
            page = await ctx.new_page()
            for q in queries:
                if browser_dead:
                    break
                for pnum in range(max_pages):
                    try:
                        url = f"https://www.google.com/search?q={quote(q)}&start={pnum * 10}"
                        if not quiet:
                            console.print(f"  [cyan]Browser[/cyan] {q[:40]}  page {pnum + 1}")
                        await page.goto(url, wait_until="networkidle", timeout=30000)
                        await _handle_consent_and_captcha(page, quiet)
                        for _ in range(3):
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(800)
                        items = await page.evaluate("""() => {
                            const out = [];
                            document.querySelectorAll('h3').forEach(h => {
                                const a = h.closest('a');
                                if (a && a.href) out.push([h.innerText, a.href]);
                            });
                            return out;
                        }""")
                        for title, link in items:
                            if title and link:
                                results.append((title.strip(), link))
                        has_next = await page.evaluate("() => document.querySelector('a#pnnext') !== null")
                        if not has_next:
                            break
                        if pnum < max_pages - 1:
                            await page.wait_for_timeout(delay * 1000)
                    except Exception as e:
                        if _is_browser_closed(e):
                            console.print("[yellow]Browser was closed. Returning results gathered so far.[/yellow]")
                            browser_dead = True
                            break
                        if not quiet:
                            console.print(f"  [yellow]Error page {pnum + 1}: {e}[/yellow]")
            if not browser_dead:
                await browser.close()
    except Exception as e:
        if not _is_browser_closed(e) and not quiet:
            console.print(f"[yellow]Browser error: {e}[/yellow]")
    return results


async def playwright_fetch_file_links(queries: list[str], max_pages: int, delay: int, quiet: bool, partial_out: list | None = None) -> list[tuple[str, str]]:
    """Returns list of (url, dork) tuples."""
    results = partial_out if partial_out is not None else []
    seen = {u for u, _ in results}
    browser_dead = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
                viewport={"width": 1600, "height": 900}, locale="en-US",
            )
            page = await ctx.new_page()
            for q in queries:
                if browser_dead:
                    break
                for pnum in range(max_pages):
                    try:
                        url = f"https://www.google.com/search?q={quote(q)}&start={pnum * 10}"
                        if not quiet:
                            console.print(f"  [cyan]Browser[/cyan] {q[:40]}  page {pnum + 1}")
                        await page.goto(url, wait_until="networkidle", timeout=30000)
                        await _handle_consent_and_captcha(page, quiet)
                        anchors = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
                        for href in anchors:
                            if href and is_file_link(href):
                                c = clean_google_url(href)
                                if c and c not in seen:
                                    seen.add(c)
                                    results.append((c, q))
                        has_next = await page.evaluate("() => document.querySelector('a#pnnext') !== null")
                        if not has_next:
                            break
                        if pnum < max_pages - 1:
                            await page.wait_for_timeout(delay * 1000)
                    except Exception as e:
                        if _is_browser_closed(e):
                            console.print("[yellow]Browser was closed. Returning results gathered so far.[/yellow]")
                            browser_dead = True
                            break
                        if not quiet:
                            console.print(f"  [yellow]Error: {e}[/yellow]")
            if not browser_dead:
                await browser.close()
    except Exception as e:
        if not _is_browser_closed(e) and not quiet:
            console.print(f"[yellow]Browser error: {e}[/yellow]")
    return results


# ---------------------------------------------------------------------------
# Name extraction & email generation
# ---------------------------------------------------------------------------

def _clean_name_token(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _add_name(first: str, last: str, raw_title: str, source: str, seen: set, out: list) -> None:
    first = _clean_name_token(first)
    last = _clean_name_token(last)
    if not first or not last or len(first) < 2 or len(last) < 2:
        return
    if first in TITLE_NOISE or last in TITLE_NOISE:
        return
    key = (first, last)
    if key not in seen:
        seen.add(key)
        out.append((first, last, raw_title, source))


def _extract_from_linkedin_title(title: str) -> tuple[str, str] | None:
    """LinkedIn titles: 'FirstName LastName - Title at Company | LinkedIn'"""
    parts = re.split(r"[-–—|]", title, maxsplit=1)
    name_part = re.sub(r"[^a-zA-Z\s]", "", (parts[0] if parts else "")).strip()
    tokens = name_part.split()
    if len(tokens) >= 2:
        return tokens[0], tokens[-1]
    return None


def _extract_from_linkedin_url(url: str) -> tuple[str, str] | None:
    """LinkedIn URLs: linkedin.com/in/firstname-lastname-hexid"""
    m = re.search(r"linkedin\.com/in/([a-zA-Z]+-[a-zA-Z]+-?)", url, re.I)
    if m:
        parts = m.group(1).rstrip("-").split("-")[:2]
        if len(parts) == 2 and all(len(p) >= 2 for p in parts):
            return parts[0], parts[1]
    return None


def _extract_from_rocketreach_title(title: str) -> tuple[str, str] | None:
    """RocketReach titles: 'FirstName LastName - Company | RocketReach'"""
    parts = re.split(r"[-–—|]", title, maxsplit=1)
    name_part = re.sub(r"[^a-zA-Z\s]", "", (parts[0] if parts else "")).strip()
    tokens = name_part.split()
    if len(tokens) >= 2:
        return tokens[0], tokens[-1]
    return None


def _extract_from_zoominfo_title(title: str) -> tuple[str, str] | None:
    """ZoomInfo people titles: 'FirstName LastName - Title - ZoomInfo'"""
    if "overview" in title.lower() or "company" in title.lower():
        return None
    parts = re.split(r"[-–—|]", title, maxsplit=1)
    name_part = re.sub(r"[^a-zA-Z\s]", "", (parts[0] if parts else "")).strip()
    tokens = name_part.split()
    if len(tokens) >= 2:
        return tokens[0], tokens[-1]
    return None


def extract_names(items: list[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    """Source-aware name extraction. Returns list of (first, last, raw_title, source)."""
    seen = set()
    out = []
    for title, link in items:
        link_lower = link.lower()
        src = _source_from_url(link)
        extracted = None

        if "linkedin.com/in/" in link_lower:
            extracted = _extract_from_linkedin_title(title)
            if not extracted:
                extracted = _extract_from_linkedin_url(link)
        elif "rocketreach.co" in link_lower:
            extracted = _extract_from_rocketreach_title(title)
        elif "zoominfo.com" in link_lower:
            extracted = _extract_from_zoominfo_title(title)
        else:
            # Generic: only extract if title clearly has "Name - Role" pattern
            parts = re.split(r"[-–—|]", title, maxsplit=1)
            if len(parts) >= 2:
                name_part = re.sub(r"[^a-zA-Z\s]", "", parts[0]).strip()
                tokens = name_part.split()
                if 2 <= len(tokens) <= 4:
                    extracted = (tokens[0], tokens[-1])

        if extracted:
            _add_name(extracted[0], extracted[1], title, src, seen, out)

        if "linkedin.com/in/" in link_lower:
            url_name = _extract_from_linkedin_url(link)
            if url_name:
                _add_name(url_name[0], url_name[1], title, src, seen, out)

    return out


def build_emails(names: list[tuple[str, str, str, str]], domain: str, fmt_id: int) -> list[dict]:
    fn = EMAIL_FORMATS.get(fmt_id, EMAIL_FORMATS[1])
    out = []
    seen = set()
    for first, last, raw_title, source in names:
        if not first or not last:
            continue
        try:
            email = fn(first, last, domain)
        except (IndexError, KeyError):
            continue
        if email not in seen:
            seen.add(email)
            out.append({
                "name": f"{first.title()} {last.title()}",
                "email": email,
                "first": first,
                "last": last,
                "raw_title": raw_title,
                "source": source,
            })
    return out


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_with_flaresolverr(url: str, flaresolverr_url: str, timeout: int = 60) -> requests.Response | None:
    try:
        r = requests.post(
            f"{flaresolverr_url.rstrip('/')}/v1",
            headers={"Content-Type": "application/json"},
            json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
            timeout=timeout + 10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            return None
        sol = data.get("solution", {})
        cookies = {c["name"]: c["value"] for c in sol.get("cookies", [])}
        ua = sol.get("userAgent", "")
        resp = requests.get(url, headers={"User-Agent": ua}, cookies=cookies, stream=True, timeout=timeout)
        resp.raise_for_status()
        return resp
    except Exception:
        return None


def run_downloads(urls, output_dir, proxy, flaresolverr_url, resume, quiet):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    success = failed = total_bytes = 0
    file_types = {}
    progress = Progress(
        SpinnerColumn(), TextColumn("[bold blue]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("Downloading", total=len(urls))
        for idx, url in enumerate(urls, 1):
            filename = None
            try:
                if flaresolverr_url:
                    resp = download_with_flaresolverr(url, flaresolverr_url)
                    if resp is None:
                        raise requests.RequestException("FlareSolverr failed")
                else:
                    resp = requests.get(url, timeout=30, stream=True, proxies=proxies)
                    resp.raise_for_status()
                cd = resp.headers.get("Content-Disposition", "")
                if cd:
                    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
                    if m:
                        filename = unquote(m.group(1), encoding="utf-8")
                    else:
                        m = re.search(r'filename=["\']?([^"\';\s]+)', cd)
                        if m:
                            filename = m.group(1).strip("\"'")
                if not filename:
                    filename = unquote(os.path.basename(urlparse(url).path), encoding="utf-8")
                ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                mime_ext = MIME_TO_EXT.get(ct, "")
                if filename:
                    cur_ext = os.path.splitext(filename)[1].lower()
                    if cur_ext in [".aspx", ".php", ".jsp", ".cgi", ".asp"] and mime_ext:
                        filename = os.path.splitext(filename)[0] + mime_ext
                if not filename or "." not in filename:
                    filename = f"file_{idx}{mime_ext}"
                filename = sanitize_filename(filename)
                out_path = os.path.join(output_dir, filename)
                if resume and os.path.exists(out_path):
                    resp.close()
                    success += 1
                    progress.advance(task)
                    continue
                if os.path.exists(out_path):
                    base, fext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(out_path):
                        filename = f"{base}_{counter}{fext}"
                        out_path = os.path.join(output_dir, filename)
                        counter += 1
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                sz = os.path.getsize(out_path)
                total_bytes += sz
                ext = os.path.splitext(filename)[1].lower()
                if ext:
                    file_types[ext] = file_types.get(ext, 0) + 1
                if not quiet:
                    progress.console.print(f"  [green]✓[/green] {filename[:50]}  {format_size(sz)}")
                success += 1
            except Exception:
                failed += 1
                if not quiet:
                    progress.console.print(f"  [red]✗[/red] {(filename or url[:50])}  FAILED")
            progress.advance(task)
    return success, failed, total_bytes, file_types


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_output(data: list[dict], path: str, fmt: str) -> None:
    if fmt == "json":
        out = json.dumps(data, indent=2, ensure_ascii=False)
    elif fmt == "csv":
        buf = io.StringIO()
        if data:
            writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()), quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(data)
        out = buf.getvalue()
    else:
        if data and "email" in data[0]:
            out = "\n".join(r["email"] for r in data)
        elif data and "url" in data[0]:
            out = "\n".join(r["url"] for r in data)
        else:
            out = "\n".join(str(r) for r in data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


def write_to_stdout(data: list[dict], fmt: str) -> None:
    if fmt == "json":
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        buf = io.StringIO()
        if data:
            writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()), quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(data)
        sys.stdout.write(buf.getvalue())
    else:
        for r in data:
            sys.stdout.write(r.get("email", r.get("url", str(r))) + "\n")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def cmd_show_examples() -> None:
    table = Table(title="Example Dorks", title_style="bold", show_lines=False)
    table.add_column("Dork", style="cyan", no_wrap=False)
    table.add_column("Description", style="dim")
    for dork, desc in DORK_EXAMPLES:
        table.add_row(dork, desc)
    console.print(table)
    console.print("\n[dim]Use {domain} and {company} as placeholders. Replaced at runtime by -d and -c flags.[/dim]")


def _resolve_source(args, api_key: str | None) -> tuple[bool, bool]:
    """Returns (use_serper, use_browser)."""
    src = getattr(args, "source", "auto")
    if src == "serper":
        if not api_key:
            console.print("[red]--source serper requires --api-key or SERPER_API_KEY[/red]")
            sys.exit(1)
        return True, False
    if src == "browser":
        return False, True
    return bool(api_key), True


# ---------------------------------------------------------------------------
# cmd_contacts
# ---------------------------------------------------------------------------

def cmd_contacts(args, cfg, api_key):
    company = getattr(args, "company", None) or cfg.get("company")
    domain = getattr(args, "domain", None) or cfg.get("domain")
    if not company or not domain:
        console.print("[red]contacts requires -c/--company and -d/--domain[/red]")
        return 1
    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)
    pages = getattr(args, "pages", 10) or cfg.get("pages", 10)
    delay = getattr(args, "delay", 3) or cfg.get("delay", 3)
    fmt_id = getattr(args, "format", 1) or cfg.get("email_format", 1)
    fmt_out = getattr(args, "format_output", "txt") or "txt"
    stdout_mode = getattr(args, "stdout", False)
    dry_run = getattr(args, "dry_run", False)
    out_dir_base = getattr(args, "output_dir", None) or cfg.get("output_dir")
    out_file = getattr(args, "output", "emails.txt")
    save_names_path = getattr(args, "save_names", None)
    use_serper, use_browser = _resolve_source(args, api_key)

    queries = [resolve_placeholders(q, domain, company) for q in CONTACT_QUERIES]

    if dry_run:
        console.print(Panel("Would run queries:\n" + "\n".join(f"  {q}" for q in queries), title="[yellow]Dry Run[/yellow]"))
        return 0

    if not quiet:
        src_label = "Serper + Browser" if (use_serper and use_browser) else ("Serper" if use_serper else "Browser")
        console.print(Panel(
            f"[bold]Company:[/bold] {company}\n[bold]Domain:[/bold] {domain}\n"
            f"[bold]Source:[/bold] {src_label}\n[bold]Email format:[/bold] {fmt_id} ({EMAIL_FORMAT_HELP.split('  ')[fmt_id - 1].strip()})\n"
            f"[bold]Queries:[/bold] {len(queries)}",
            title="[bold cyan]scry — contacts[/bold cyan]", border_style="cyan",
        ))

    if not api_key and use_browser:
        console.print(Panel(NO_API_KEY_MSG, border_style="yellow"))

    start_time = time.time()
    all_items = []

    abort = False
    if use_serper and api_key:
        for qi, q in enumerate(queries, 1):
            if abort:
                break
            if not quiet:
                console.print(f"[cyan]Serper [{qi}/{len(queries)}][/cyan] {q}")
            try:
                data = serper_fetch_organic(q, api_key, pages)
                for row in data:
                    title = row.get("title", "")
                    href = row.get("link") or row.get("url", "")
                    if title or href:
                        all_items.append((title, href))
                if not quiet:
                    console.print(f"  [dim]{len(data)} results[/dim]")
            except KeyboardInterrupt:
                if not _ask_continue():
                    abort = True
                else:
                    console.print(f"[yellow]Skipped: {q[:50]}[/yellow]")

    if use_browser and not abort:
        if not quiet:
            console.print("\n[bold]Browser gathering...[/bold]")
        browser_partial = []
        try:
            asyncio.run(playwright_fetch_titles(queries, pages, delay, quiet, browser_partial))
        except KeyboardInterrupt:
            console.print(f"[yellow]Browser interrupted. Kept {len(browser_partial)} results gathered so far.[/yellow]")
            if not _ask_continue():
                abort = True
        all_items.extend(browser_partial)

    names = extract_names(all_items)
    emails = build_emails(names, domain, fmt_id)
    elapsed = time.time() - start_time

    if not emails:
        if all_items:
            console.print(f"[yellow]Gathered {len(all_items)} raw results but no valid names extracted.[/yellow]")
        else:
            console.print("[yellow]No results found. Try broadening the query or check domain/company.[/yellow]")
        return 0

    console.print(f"\n[bold]{len(emails)} unique emails from {len(all_items)} raw results[/bold]")

    if stdout_mode:
        write_to_stdout(emails, fmt_out)
        return 0

    if out_dir_base:
        run_dir = make_run_dir(out_dir_base, "contacts", company)
        write_output(emails, os.path.join(run_dir, f"emails.{fmt_out}"), fmt_out)
        write_output(emails, os.path.join(run_dir, "emails.txt"), "txt")
        titles_path = os.path.join(run_dir, "raw_titles.txt")
        with open(titles_path, "w", encoding="utf-8") as f:
            for e in emails:
                f.write(e.get("raw_title", "") + "\n")
        if save_names_path or True:
            names_path = os.path.join(run_dir, "names.txt")
            with open(names_path, "w", encoding="utf-8") as f:
                for e in emails:
                    f.write(e["name"] + "\n")
        log_lines = [
            "scry contacts run",
            f"Date: {datetime.now().isoformat()}",
            f"Company: {company}",
            f"Domain: {domain}",
            f"Source: {'Serper + Browser' if (use_serper and use_browser) else ('Serper' if use_serper else 'Browser')}",
            f"Queries: {len(queries)}",
            f"Raw results: {len(all_items)}",
            f"Unique names: {len(names)}",
            f"Emails generated: {len(emails)}",
            f"Email format: {fmt_id}",
            f"Elapsed: {elapsed:.2f}s",
        ]
        write_run_log(run_dir, log_lines)
        console.print(f"\n[green]Results saved to {run_dir}/[/green]")
    else:
        write_output(emails, out_file, fmt_out)
        if save_names_path:
            with open(save_names_path, "w", encoding="utf-8") as f:
                for e in emails:
                    f.write(e["name"] + "\n")
            console.print(f"[green]Names saved to {save_names_path}[/green]")
        console.print(f"[green]Saved to {out_file}[/green]")

    if not quiet:
        t = Table(title="Summary", show_lines=False)
        t.add_column("Metric", style="cyan")
        t.add_column("Value", style="green")
        t.add_row("Source", "Serper + Browser" if (use_serper and use_browser) else ("Serper" if use_serper else "Browser"))
        t.add_row("Raw results", str(len(all_items)))
        t.add_row("Unique names", str(len(names)))
        t.add_row("Emails", str(len(emails)))
        t.add_row("Format", str(fmt_id))
        t.add_row("Domain", domain)
        t.add_row("Elapsed", f"{elapsed:.2f}s")
        console.print(t)
    return 0


# ---------------------------------------------------------------------------
# cmd_files
# ---------------------------------------------------------------------------

def cmd_files(args, cfg, api_key):
    input_file = getattr(args, "input_file", None)
    queries_arg = getattr(args, "query", []) or []
    dorks_file = getattr(args, "dorks_file", None)
    domain = getattr(args, "domain", None) or cfg.get("domain")
    company = getattr(args, "company", None) or cfg.get("company")
    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)
    pages = getattr(args, "pages", 10) or cfg.get("pages", 10)
    delay = getattr(args, "delay", 3) or cfg.get("delay", 3)
    fmt_out = getattr(args, "format_output", "txt") or "txt"
    stdout_mode = getattr(args, "stdout", False)
    dry_run = getattr(args, "dry_run", False)
    out_dir_base = getattr(args, "output_dir", None) or cfg.get("output_dir")
    out_file = getattr(args, "output", "file_links.txt")
    do_download = getattr(args, "download", False)
    download_dir = getattr(args, "download_dir", "downloads") or cfg.get("download_dir", "downloads")
    proxy = getattr(args, "proxy", None) or cfg.get("proxy")
    flaresolverr = getattr(args, "flaresolverr", None) or cfg.get("flaresolverr")
    resume = not getattr(args, "no_resume", False)
    use_serper, use_browser = _resolve_source(args, api_key)

    if input_file:
        try:
            with open(input_file, encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            console.print(f"[red]File not found: {input_file}[/red]")
            return 1
        if not urls:
            console.print("[yellow]No URLs in file.[/yellow]")
            return 0
        file_results = [{"url": u, "filename": (u.rsplit("/", 1)[-1] or "file")[:80], "dork": "(input-file)"} for u in urls]
    else:
        dorks = []
        if dorks_file:
            try:
                with open(dorks_file, encoding="utf-8") as f:
                    dorks.extend(line.strip() for line in f if line.strip() and not line.strip().startswith("#"))
            except FileNotFoundError:
                console.print(f"[red]Dorks file not found: {dorks_file}[/red]")
                return 1
        dorks.extend(queries_arg)
        if not dorks:
            console.print("[red]Provide -q/--query or --dorks-file (or --input-file to download)[/red]")
            return 1
        try:
            resolved = [resolve_placeholders(d, domain, company) for d in dorks]
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return 1

        if dry_run:
            console.print(Panel("Would run dorks:\n" + "\n".join(f"  {r}" for r in resolved), title="[yellow]Dry Run[/yellow]"))
            return 0

        if not quiet:
            src_label = "Serper + Browser" if (use_serper and use_browser) else ("Serper" if use_serper else "Browser")
            console.print(Panel(
                f"[bold]Dorks:[/bold] {len(resolved)}\n[bold]Source:[/bold] {src_label}\n[bold]Pages/dork:[/bold] {pages}",
                title="[bold cyan]scry — files[/bold cyan]", border_style="cyan",
            ))

        if not api_key and use_browser:
            console.print(Panel(NO_API_KEY_MSG, border_style="yellow"))

        start_time = time.time()
        all_links = []
        seen = set()

        abort = False
        if use_serper and api_key:
            for qi, q in enumerate(resolved, 1):
                if abort:
                    break
                if not quiet:
                    console.print(f"[cyan]Serper [{qi}/{len(resolved)}][/cyan] {q}")
                try:
                    results = serper_fetch_file_links(q, api_key, pages)
                    for url, dork in results:
                        if url not in seen:
                            seen.add(url)
                            all_links.append((url, dork))
                    if not quiet:
                        console.print(f"  [dim]{len(results)} links[/dim]")
                except KeyboardInterrupt:
                    if not _ask_continue():
                        abort = True
                    else:
                        console.print(f"[yellow]Skipped: {q[:50]}[/yellow]")

        if use_browser and not abort:
            if not quiet:
                console.print("\n[bold]Browser gathering...[/bold]")
            browser_partial = []
            try:
                asyncio.run(playwright_fetch_file_links(resolved, pages, delay, quiet, browser_partial))
            except KeyboardInterrupt:
                console.print(f"[yellow]Browser interrupted. Kept {len(browser_partial)} links gathered so far.[/yellow]")
                if not _ask_continue():
                    abort = True
            for url, dork in browser_partial:
                if url not in seen:
                    seen.add(url)
                    all_links.append((url, dork))

        elapsed = time.time() - start_time
        file_results = [{"url": u, "filename": (u.rsplit("/", 1)[-1] or "file")[:80], "dork": d} for u, d in all_links]
        if all_links and not quiet:
            console.print(f"\n[bold]{len(all_links)} unique file links found[/bold]")

    if not file_results:
        console.print("[yellow]No file links found. Try broadening the query.[/yellow]")
        return 0

    if stdout_mode:
        write_to_stdout(file_results, fmt_out)
        return 0

    if out_dir_base:
        label = domain or company or "dork"
        run_dir = make_run_dir(out_dir_base, "files", label)
        write_output(file_results, os.path.join(run_dir, f"file_links.{fmt_out}"), fmt_out)
        write_output(file_results, os.path.join(run_dir, "file_links.txt"), "txt")
        actual_download_dir = os.path.join(run_dir, "downloads") if do_download else None
        src_label = "Serper + Browser" if (use_serper and use_browser) else ("Serper" if use_serper else "Browser")
        log_lines = [
            "scry files run",
            f"Date: {datetime.now().isoformat()}",
            f"Domain: {domain or 'N/A'}",
            f"Company: {company or 'N/A'}",
            f"Source: {src_label}",
            f"Dorks: {len(resolved)}",
            f"File links found: {len(file_results)}",
            f"Elapsed: {elapsed:.2f}s",
        ]
        write_run_log(run_dir, log_lines)
        console.print(f"\n[green]Results saved to {run_dir}/[/green]")
    else:
        write_output(file_results, out_file, fmt_out)
        actual_download_dir = download_dir if do_download else None
        console.print(f"[green]Saved {len(file_results)} links to {out_file}[/green]")

    if actual_download_dir:
        console.print(f"\n[bold]Downloading {len(file_results)} files to {actual_download_dir}[/bold]")
        dl_start = time.time()
        urls_list = [r["url"] for r in file_results]
        succ, fail, total_bytes, ftypes = run_downloads(urls_list, actual_download_dir, proxy, flaresolverr, resume, quiet)
        dl_elapsed = time.time() - dl_start
        avg_speed = total_bytes / dl_elapsed if dl_elapsed > 0 else 0
        if not quiet:
            t = Table(title="Download Statistics", show_lines=False)
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="green")
            t.add_row("Total files", str(len(urls_list)))
            t.add_row("Success", str(succ))
            t.add_row("Failed", str(fail))
            t.add_row("Total size", format_size(total_bytes))
            t.add_row("Elapsed", f"{dl_elapsed:.2f}s")
            t.add_row("Avg speed", f"{format_size(avg_speed)}/s")
            for ext, count in sorted(ftypes.items(), key=lambda x: x[1], reverse=True):
                t.add_row(f"  {ext}", f"{count} file{'s' if count != 1 else ''}")
            console.print(t)

    if not quiet and not input_file:
        t = Table(title="Search Summary", show_lines=False)
        t.add_column("Metric", style="cyan")
        t.add_column("Value", style="green")
        t.add_row("File links", str(len(file_results)))
        t.add_row("Source", "Serper + Browser" if (use_serper and use_browser) else ("Serper" if use_serper else "Browser"))
        console.print(t)

    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

BANNER = """
  ╭──────────────────────────────────╮
  │  scry — osint & dorking toolkit  │
  │  contacts · files · download     │
  ╰──────────────────────────────────╯
"""

EPILOG = """
NOTE: All flags go AFTER the subcommand (contacts/files).

  Contacts:
  ─────────────────────────────────────────────────────────────────
  %(prog)s contacts -c "Acme" -d acme.com --api-key KEY
  %(prog)s contacts -c "Acme" -d acme.com -f 3 --save-names names.txt
  %(prog)s contacts -c "Acme" -d acme.com --source serper --api-key KEY
  %(prog)s contacts -c "Acme" -d acme.com --output-dir output

  Files:
  ─────────────────────────────────────────────────────────────────
  %(prog)s files -d acme.com -q "site:{domain} filetype:pdf" --api-key KEY
  %(prog)s files --dorks-file dorks.txt -d acme.com -c "Acme" --download
  %(prog)s files --input-file links.txt --download --download-dir out
  %(prog)s files -d acme.com --dorks-file dorks.txt --output-dir output

  Output:
  ─────────────────────────────────────────────────────────────────
  %(prog)s contacts -c "Acme" -d acme.com --format-output json --stdout
  %(prog)s files -d acme.com -q "site:{domain} filetype:pdf" --format-output csv

  Dork examples:
  ─────────────────────────────────────────────────────────────────
  %(prog)s --show-examples

Email formats (contacts -f N):
  1=first.last  2=firstlast  3=flast  4=first  5=last
  6=last.first  7=first_last  8=f.last  9=firstl  10=first.last1
"""


def main() -> int:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--api-key", metavar="KEY", help="Serper API key (or set SERPER_API_KEY env)")
    shared.add_argument("--config", metavar="PATH", help="YAML config (~/.scry.yaml)")
    shared.add_argument("--quiet", action="store_true", help="Minimal output")
    shared.add_argument("--verbose", action="store_true", help="Verbose output")
    shared.add_argument("--format-output", choices=["txt", "json", "csv"], default="txt", help="Output format (default: txt)")
    shared.add_argument("--stdout", action="store_true", help="Print to stdout (pipe-friendly)")
    shared.add_argument("--dry-run", action="store_true", help="Show what would run, no execution")
    shared.add_argument("--output-dir", metavar="DIR", help="Structured output directory (timestamped per run)")
    shared.add_argument("--source", choices=["auto", "serper", "browser"], default="auto",
                        help="Data source: auto (both if key), serper (API only), browser (scrape only)")

    parser = argparse.ArgumentParser(
        description=BANNER, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--show-examples", action="store_true", help="Print example dorks and exit")
    sub = parser.add_subparsers(dest="cmd")

    p_c = sub.add_parser("contacts", parents=[shared], help="Gather names, generate emails",
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p_c.add_argument("-c", "--company", required=True, help="Company name")
    p_c.add_argument("-d", "--domain", required=True, help="Domain (e.g. acme.com)")
    p_c.add_argument("-f", "--format", type=int, choices=range(1, 11), default=1, metavar="N",
                     help=f"Email format 1-10. {EMAIL_FORMAT_HELP}")
    p_c.add_argument("-o", "--output", default="emails.txt", metavar="FILE")
    p_c.add_argument("--save-names", metavar="FILE", help="Also save names to file")
    p_c.add_argument("-p", "--pages", type=int, default=10, metavar="N", help="Max pages per query (default: 10)")
    p_c.add_argument("--delay", type=int, default=3, metavar="SEC")

    p_f = sub.add_parser("files", parents=[shared], help="Dork for files, download",
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    p_f.add_argument("-q", "--query", action="append", metavar="DORK", help="Dork (repeatable)")
    p_f.add_argument("--dorks-file", metavar="PATH", help="File with dorks (one per line)")
    p_f.add_argument("-c", "--company", metavar="NAME", help="Replaces {company}")
    p_f.add_argument("-d", "--domain", metavar="DOMAIN", help="Replaces {domain}")
    p_f.add_argument("-o", "--output", default="file_links.txt", metavar="FILE")
    p_f.add_argument("--input-file", metavar="PATH", help="Skip search, download URLs from file")
    p_f.add_argument("--download", action="store_true", help="Download found files")
    p_f.add_argument("--download-dir", default="downloads", metavar="DIR")
    p_f.add_argument("-p", "--pages", type=int, default=10, metavar="N", help="Max pages per dork (default: 10)")
    p_f.add_argument("--delay", type=int, default=3, metavar="SEC")
    p_f.add_argument("--proxy", metavar="URL")
    p_f.add_argument("--flaresolverr", metavar="URL")
    p_f.add_argument("--no-resume", action="store_true")

    args = parser.parse_args()

    if getattr(args, "show_examples", False):
        cmd_show_examples()
        return 0
    if not args.cmd:
        parser.print_help()
        return 0

    cfg = load_config(getattr(args, "config", None))
    api_key = getattr(args, "api_key", None) or os.environ.get("SERPER_API_KEY") or cfg.get("api_key")

    if args.cmd == "contacts":
        return cmd_contacts(args, cfg, api_key)
    if args.cmd == "files":
        return cmd_files(args, cfg, api_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
