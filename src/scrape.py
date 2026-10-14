"""Diff-driven micro-batch: detect new Court of Chancery opinions, summarize, accumulate.

Flow per run (every 2h on weekdays):
  1. GET the opinions listing and parse the table into normalized rows.
  2. Fingerprint the *normalized rows* (not raw HTML: ASP.NET pages embed a
     per-request __VIEWSTATE that would change the hash on every fetch).
  3. Fingerprint unchanged and no backlog -> exit immediately (changed=false).
  4. Otherwise diff rows against processed_cases.json by case_id, download each
     new opinion, summarize it, and append the result to daily_accumulation.json.

Usage:  python src/scrape.py
Env:    ANTHROPIC_API_KEY (required when there is something to summarize)
        MAX_CASES_PER_RUN   (default 8)   - per-run cost ceiling
        RUN_TOKEN_BUDGET    (default 400000) - per-run input-token ceiling
        BACKFILL            (default 0)   - on first run, summarize this many
                                            existing rows instead of seeding all
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import (
    ACCUMULATION_PATH,
    NY,
    PROCESSED_PATH,
    env_int,
    load_json,
    now_ny,
    save_json,
    set_github_output,
    today_ny,
)

LISTING_URL = "https://courts.delaware.gov/opinions/index.aspx?ag=court%20of%20chancery"
USER_AGENT = (
    "Mozilla/5.0 (compatible; ChanceryDigestBot/1.0; personal research digest; "
    "+https://github.com/)"
)
HTTP_TIMEOUT = (10, 60)  # (connect, read) seconds
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_FAILURES_PER_CASE = 3
PROCESSED_RETENTION_DAYS = 400

# Header text -> field. Checked in order; first match wins, so the more specific
# "case number" patterns are tested before the generic "case" one.
HEADER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("docket", re.compile(r"docket|case\s*(no|num|#)|c\.?\s*a\.?\s*no|number", re.I)),
    ("judge", re.compile(r"judge|judicial|officer|author|chancellor|magistrate|justice", re.I)),
    ("date", re.compile(r"date|issued|decided|released|filed", re.I)),
    ("opinion_type", re.compile(r"\btype\b|document", re.I)),
    ("name", re.compile(r"case|caption|title|name|parties", re.I)),
]
DOCKET_RE = re.compile(r"\b(\d{4}-\d{3,5}-[A-Z]{2,6}|\d{3,6}-[A-Z]{2,6})\b")
DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y")


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    name: str
    docket: str
    date: str          # ISO date as published by the court (already a NY calendar date)
    judge: str
    opinion_type: str
    url: str


# --------------------------------------------------------------------------- HTTP

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def get_with_retry(session: requests.Session, url: str, *, stream: bool = False) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT, stream=stream)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"GET {url} failed after retries: {last_exc}")


# ------------------------------------------------------------------------ parsing

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_date(raw: str) -> str:
    raw = _clean(raw)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", raw)
    if m:
        return _parse_date(m.group(0))
    return raw  # keep the court's text rather than invent a date


def _map_headers(header_cells: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    taken: set[str] = set()
    for idx, text in enumerate(header_cells):
        for field, pattern in HEADER_PATTERNS:
            if field not in taken and pattern.search(text):
                mapping[idx] = field
                taken.add(field)
                break
    return mapping


def _case_id(url: str, docket: str, date: str, name: str) -> str:
    """Stable, timezone-independent identity for an opinion."""
    if url:
        qs = parse_qs(urlparse(url).query)
        for key in ("id", "ID", "Id"):
            if qs.get(key):
                return f"de-{qs[key][0]}"
    digest = hashlib.sha256(f"{docket}|{date}|{name}".encode()).hexdigest()[:16]
    return f"h-{digest}"


def parse_listing(html: str, base_url: str = LISTING_URL) -> list[CaseRow]:
    """Locate the opinions table by its headers (not by position or CSS id) and
    extract Case Name, Docket Number, Date and Judge from each row."""
    soup = BeautifulSoup(html, "html.parser")
    best: list[CaseRow] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = [_clean(c.get_text(" ")) for c in rows[0].find_all(["th", "td"])]
        mapping = _map_headers(header_cells)
        if "name" not in mapping.values() and "docket" not in mapping.values():
            continue
        parsed: list[CaseRow] = []
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            fields = {f: "" for f in ("name", "docket", "date", "judge", "opinion_type")}
            for idx, cell in enumerate(cells):
                if idx in mapping:
                    fields[mapping[idx]] = _clean(cell.get_text(" "))
            link = tr.find("a", href=True)
            url = urljoin(base_url, link["href"]) if link else ""
            if not fields["name"] and link:
                fields["name"] = _clean(link.get_text(" "))
            if not fields["docket"]:
                m = DOCKET_RE.search(tr.get_text(" "))
                fields["docket"] = m.group(1) if m else ""
            if not fields["judge"]:
                m = DOCKET_RE.search(fields["docket"])
                suffix = m.group(1).rsplit("-", 1)[-1] if m else ""
                fields["judge"] = f"Unknown (docket suffix {suffix})" if suffix else "Unknown"
            fields["date"] = _parse_date(fields["date"])
            if not (fields["name"] or fields["docket"]) or not url:
                continue
            parsed.append(
                CaseRow(
                    case_id=_case_id(url, fields["docket"], fields["date"], fields["name"]),
                    url=url,
                    **fields,
                )
            )
        if len(parsed) > len(best):
            best = parsed
    # De-duplicate (a row can repeat across nested tables); keep first occurrence.
    seen: set[str] = set()
    unique = [r for r in best if not (r.case_id in seen or seen.add(r.case_id))]
    return unique


def fingerprint(rows: list[CaseRow]) -> str:
    canonical = json.dumps(
        sorted((asdict(r) for r in rows), key=lambda d: d["case_id"]),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ------------------------------------------------------------- text extraction

def extract_text(session: requests.Session, url: str) -> str:
    resp = get_with_retry(session, url, stream=True)
    buf = io.BytesIO()
    for chunk in resp.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"Opinion exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
    data = buf.getvalue()
    ctype = resp.headers.get("Content-Type", "").lower()

    if data[:5] == b"%PDF-" or "pdf" in ctype:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(pages)
    elif "html" in ctype or data.lstrip()[:1] == b"<":
        text = BeautifulSoup(data, "html.parser").get_text("\n")
    else:
        raise RuntimeError(f"Unsupported opinion format ({ctype or 'unknown'}): {url}")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 200:
        raise RuntimeError("Extracted text is empty or near-empty (likely a scanned PDF).")
    return text


# ------------------------------------------------------------------ pipeline

def _prune(cases: dict) -> dict:
    cutoff = (now_ny() - timedelta(days=PROCESSED_RETENTION_DAYS)).isoformat()
    return {k: v for k, v in cases.items() if v.get("processed_at", "") >= cutoff}


def _accumulate(accum: dict, entry: dict) -> bool:
    """Append once per case_id. Returns False if already present (idempotent)."""
    if any(e["case_id"] == entry["case_id"] for e in accum["entries"]):
        return False
    accum["entries"].append(entry)
    return True


def main() -> int:
    max_cases = env_int("MAX_CASES_PER_RUN", 8)
    run_token_budget = env_int("RUN_TOKEN_BUDGET", 400_000)
    backfill = env_int("BACKFILL", 0)

    state = load_json(PROCESSED_PATH, {"page_hash": None, "cases": {}})
    state.setdefault("cases", {})
    state.setdefault("failures", {})
    accum = load_json(ACCUMULATION_PATH, {"ny_date": None, "entries": []})
    accum.setdefault("entries", [])

    session = http_session()
    html = get_with_retry(session, LISTING_URL).text
    rows = parse_listing(html)
    if not rows:
        # Layout changed or the site served an error page. Fail loudly so the
        # Actions run goes red and GitHub e-mails you, rather than silently
        # recording an empty fingerprint.
        print("ERROR: no opinion rows parsed from listing page.", file=sys.stderr)
        return 2

    fp = fingerprint(rows)
    new_rows = [r for r in rows if r.case_id not in state["cases"]]

    # Fast path: identical table and nothing left over from a capped run.
    if fp == state.get("page_hash") and not new_rows:
        print(f"[{now_ny():%Y-%m-%d %H:%M %Z}] No change ({len(rows)} rows). Exiting.")
        set_github_output("changed", "false")
        return 0

    stamp = now_ny().isoformat(timespec="seconds")

    # First run: seed state instead of summarizing the whole back catalogue.
    if not state["cases"]:
        seed = rows[backfill:] if backfill else rows
        for r in seed:
            state["cases"][r.case_id] = {"processed_at": stamp, "status": "seeded",
                                         "docket": r.docket, "name": r.name}
        new_rows = rows[:backfill]
        print(f"Seeded {len(seed)} existing rows; backfilling {len(new_rows)}.")

    from summarize import summarize_opinion  # imported lazily: fast path needs no SDK

    tokens_used = 0
    processed_now = 0
    for row in new_rows[:max_cases]:
        if tokens_used >= run_token_budget:
            print(f"Run token budget reached ({tokens_used}); deferring the rest.")
            break

        # Crash-recovery: summary already accumulated but state not saved.
        if any(e["case_id"] == row.case_id for e in accum["entries"]):
            state["cases"][row.case_id] = {"processed_at": stamp, "status": "ok",
                                           "docket": row.docket, "name": row.name}
            save_json(PROCESSED_PATH, state)
            continue

        print(f"Processing {row.case_id}: {row.name} ({row.docket})")
        entry = {**asdict(row), "ny_processed_at": stamp, "ny_date": today_ny()}
        try:
            text = extract_text(session, row.url)
            result = summarize_opinion(text, row)
            tokens_used += result.pop("_input_tokens", 0)
            entry["summary"] = result
            status = "ok"
        except Exception as exc:  # noqa: BLE001 - one bad opinion must not sink the batch
            prev = state["failures"].get(row.case_id, 0)
            fails = (prev["count"] if isinstance(prev, dict) else prev) + 1
            # Persist the reason so failures are diagnosable from the repo alone.
            state["failures"][row.case_id] = {
                "count": fails,
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "at": stamp,
            }
            print(f"  failed ({fails}/{MAX_FAILURES_PER_CASE}): {exc}", file=sys.stderr)
            if fails < MAX_FAILURES_PER_CASE:
                save_json(PROCESSED_PATH, state)
                continue  # retried next run; page_hash stays stale so we come back
            entry["error"] = f"Automatic summary unavailable: {exc}"
            status = "failed"

        # Order matters: accumulation first, then mark processed. A crash between
        # the two is healed by the crash-recovery branch above without re-billing.
        _accumulate(accum, entry)
        accum["ny_date"] = accum.get("ny_date") or today_ny()
        save_json(ACCUMULATION_PATH, accum)
        state["cases"][row.case_id] = {"processed_at": stamp, "status": status,
                                       "docket": row.docket, "name": row.name}
        state["failures"].pop(row.case_id, None)
        save_json(PROCESSED_PATH, state)
        processed_now += 1

    backlog = [r for r in rows if r.case_id not in state["cases"]]
    if not backlog:
        state["page_hash"] = fp  # only now is the fast path allowed to skip this page
    state["cases"] = _prune(state["cases"])
    state["last_checked_ny"] = stamp
    save_json(PROCESSED_PATH, state)

    print(f"Processed {processed_now}; backlog {len(backlog)}; input tokens {tokens_used}.")
    set_github_output("changed", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
