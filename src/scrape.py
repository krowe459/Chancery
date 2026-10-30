"""Diff-driven micro-batch: detect new opinions, summarize, accumulate.

Courts watched (COURTS):
  chancery  every Court of Chancery opinion and order
  supreme   Supreme Court of Delaware decisions on appeal from the Court of
            Chancery (the caption's "Court Below", read from the PDF)
  superior  Superior Court Complex Commercial Litigation Division (CCLD)
            decisions (CCLD in the listing row or the PDF caption)

Flow per run (every 2h on weekdays):
  1. GET each court's opinions listing and parse the table into normalized rows.
  2. Fingerprint the *normalized rows* (not raw HTML: ASP.NET pages embed a
     per-request __VIEWSTATE that would change the hash on every fetch).
  3. Every fingerprint unchanged and no backlog -> exit immediately (changed=false).
  4. Otherwise diff rows against processed_cases.json by case_id. For the
     Supreme and Superior courts, read each new row's caption to decide whether
     it is in scope; out-of-scope rows are recorded and never summarized.
     Download each new in-scope opinion, summarize it, and append the result
     to daily_accumulation.json.
  A court watched for the first time is seeded (its current rows recorded,
  not summarized), as on a first run. A listing that stays empty after its
  retries does not stop the other courts, but the run exits 2 (red).

Usage:  python src/scrape.py
Env:    ANTHROPIC_API_KEY (required when there is something to summarize)
        MAX_CASES_PER_RUN   (default 8)   - per-run cost ceiling
        RUN_TOKEN_BUDGET    (default 400000) - per-run input-token ceiling
        RUN_TIME_BUDGET_MINUTES (default 40) - stop starting new opinions after
                                            this long and leave the rest for the
                                            next run (the workflow step is killed
                                            at 50 minutes)
        BACKFILL            (default 0)   - on first run, summarize this many
                                            existing rows instead of seeding all
        LISTING_RETRIES     (default 2)   - re-fetch an empty listing this many
                                            times before failing the run
        LISTING_RETRY_WAIT  (default 60)  - seconds between those re-fetches
        MAX_AGE_BUSINESS_DAYS (default 2) - skip opinions the court posts more
                                            than this many business days after
                                            their decision date (-1 disables)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
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
SUPREME_LISTING_URL = "https://courts.delaware.gov/opinions/index.aspx?ag=supreme%20court"
SUPERIOR_LISTING_URL = "https://courts.delaware.gov/opinions/index.aspx?ag=superior%20court"
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
CCLD_RE = re.compile(r"\bCCLD\b")
# A Supreme Court caption names the court below ("Court Below: Court of
# Chancery of the State of Delaware, C.A. No. 2024-0123-JTL"). A Chancery-style
# docket in the caption is the fallback signal.
COURT_BELOW_RE = re.compile(r"court\s+below\W{0,6}(?:the\s+)?court\s+of\s+chancery"
                            r"|appeal\s+from\s+the\s+court\s+of\s+chancery", re.I)
CHANCERY_DOCKET_RE = re.compile(r"C\.\s?A\.\s+No(?:s)?\.?\s+\d{4}-\d{3,5}-[A-Z]{2,6}\b")
CAPTION_CHARS = 2500   # how much of page 1 counts as the caption
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
    court: str = "chancery"   # key into COURTS


def in_scope_superior(row: CaseRow, caption) -> bool:
    """CCLD cases carry "CCLD" in the case number; read the caption only when
    the listing row does not show it."""
    if CCLD_RE.search(f"{row.docket} {row.name}"):
        return True
    return bool(CCLD_RE.search(caption()))


def in_scope_supreme(row: CaseRow, caption) -> bool:
    """Appeals from the Court of Chancery, by the caption's court below."""
    text = caption()
    return bool(COURT_BELOW_RE.search(text) or CHANCERY_DOCKET_RE.search(text))


@dataclass(frozen=True)
class Court:
    key: str
    label: str
    listing_url: str
    in_scope: object = None   # (row, caption) -> bool; None takes every row


COURTS = (
    Court("chancery", "Court of Chancery", LISTING_URL),
    Court("supreme", "Supreme Court", SUPREME_LISTING_URL, in_scope_supreme),
    Court("superior", "Superior Court (CCLD)", SUPERIOR_LISTING_URL, in_scope_superior),
)


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


def parse_listing(html: str, base_url: str = LISTING_URL, court: str = "chancery") -> list[CaseRow]:
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
                    court=court,
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

SCANNED_NOTE = ("Scanned PDF with no text layer: Claude transcribed the opinion from its page "
                "images before summarizing, so quotations and citations rest on that "
                "transcription.")


class ScannedPDF(RuntimeError):
    """A PDF with no usable text layer (an image scan). Carries the bytes so
    the caller can have Claude transcribe it (the Oct. 28 Jones v. SBC letter
    was a three-page scan that produced no summary)."""

    def __init__(self, data: bytes):
        super().__init__("Extracted text is empty or near-empty (likely a scanned PDF).")
        self.data = data


def _download(session: requests.Session, url: str) -> tuple[bytes, str]:
    resp = get_with_retry(session, url, stream=True)
    buf = io.BytesIO()
    for chunk in resp.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"Opinion exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
    return buf.getvalue(), resp.headers.get("Content-Type", "").lower()


def caption_text(session: requests.Session, url: str) -> str:
    """The start of page 1 (the caption), for scope checks. Plain extraction
    only: this runs for every new Supreme and Superior row."""
    data, ctype = _download(session, url)
    if data[:5] == b"%PDF-" or "pdf" in ctype:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = (reader.pages[0].extract_text() or "") if reader.pages else ""
    else:
        text = BeautifulSoup(data, "html.parser").get_text("\n")
    return re.sub(r"\s+", " ", text)[:CAPTION_CHARS]


def extract_text(session: requests.Session, url: str) -> str:
    data, ctype = _download(session, url)
    is_pdf = data[:5] == b"%PDF-" or "pdf" in ctype

    if is_pdf:
        from pypdf import PdfReader

        from pdftext import pdf_to_text

        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(pages)
        # Footnote-aware text when the layout allows it; plain text otherwise.
        text = pdf_to_text(data, text) or text
    elif "html" in ctype or data.lstrip()[:1] == b"<":
        text = BeautifulSoup(data, "html.parser").get_text("\n")
    else:
        raise RuntimeError(f"Unsupported opinion format ({ctype or 'unknown'}): {url}")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 200:
        if is_pdf:
            raise ScannedPDF(data)
        raise RuntimeError("Extracted text is empty or near-empty.")
    return text


def opinion_text(session: requests.Session, url: str) -> tuple[str, int, str]:
    """(text, input tokens spent getting it, coverage note). A scanned PDF is
    transcribed by Claude, which reads its page images; that costs one extra
    request and is noted on the summary."""
    try:
        return extract_text(session, url), 0, ""
    except ScannedPDF as scan:
        from summarize import transcribe_pdf

        print("  scanned PDF: transcribing with Claude")
        text, used = transcribe_pdf(scan.data)
        return text, used, SCANNED_NOTE


def add_note(result: dict, note: str) -> None:
    if note:
        result["coverage_note"] = " ".join(x for x in (note, result.get("coverage_note")) if x)


# ------------------------------------------------------------------ pipeline

def describe_page(resp) -> str:
    """One-line description of a page that yielded no rows, for the log."""
    html = resp.text or ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = _clean(BeautifulSoup(match.group(1), "html.parser").get_text()) if match else ""
    return (f"HTTP {resp.status_code}, {len(html):,} bytes, "
            f"title {title[:120]!r}" if title else
            f"HTTP {resp.status_code}, {len(html):,} bytes, no <title>")


def fetch_listing(session: requests.Session, retries: int, wait: float,
                  court: Court = COURTS[0]) -> list[CaseRow]:
    """Fetch and parse the listing, re-fetching when it parses to no rows.

    The court's site occasionally serves a page without the opinions table (an
    error or maintenance page, or a partial load); the Oct. 19, 2026 5:45 PM
    run failed that way and the next run was fine. Waiting and trying again
    rides out those blips, while a real layout change still fails the run
    after the last attempt. Each empty attempt is logged with the page's
    status, size and title, so a failed run shows what the court served."""
    for attempt in range(retries + 1):
        resp = get_with_retry(session, court.listing_url)
        rows = parse_listing(resp.text, court.listing_url, court.key)
        if rows:
            if attempt:
                print(f"{court.label} listing parsed on attempt {attempt + 1}.")
            return rows
        print(f"No {court.label} rows parsed (attempt {attempt + 1} of {retries + 1}): "
              f"{describe_page(resp)}", file=sys.stderr)
        if attempt < retries:
            time.sleep(wait)
    return []


def business_days_old(decided: str, today: date) -> int | None:
    """Weekdays after `decided` up to and including `today` (court holidays
    are not excluded). None if the listing's date could not be parsed."""
    try:
        day = date.fromisoformat(decided)
    except (TypeError, ValueError):
        return None
    count = 0
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def skip_late_postings(rows: list, state: dict, stamp: str, max_age: int,
                       today: date) -> list:
    """Drop rows decided more than `max_age` business days before `today`,
    recording them in state as "skipped_old" so they are never summarized.
    The court sometimes posts opinions days or weeks after they were decided
    (e.g. an Oct. 1 opinion first listed Oct. 16); those are not news."""
    if max_age < 0:
        return rows
    fresh = []
    for r in rows:
        age = business_days_old(r.date, today)
        if age is not None and age > max_age:
            state["cases"][r.case_id] = {"processed_at": stamp, "status": "skipped_old",
                                         "docket": r.docket, "name": r.name, "date": r.date}
            print(f"Skipping {r.name} ({r.docket}): decided {r.date}, "
                  f"{age} business days before it was listed.")
        else:
            fresh.append(r)
    return fresh


def check_scope(rows: list, court: Court, state: dict, stamp: str, session) -> list:
    """Keep the rows `court` covers. Rows outside it are recorded as
    "out_of_scope" so their PDFs are read only once. A caption that cannot be
    read is retried next run, and recorded as "scope_check_failed" after
    MAX_FAILURES_PER_CASE tries."""
    if court.in_scope is None:
        return rows
    kept = []
    for r in rows:
        try:
            ok = court.in_scope(r, lambda: caption_text(session, r.url))
        except Exception as exc:  # noqa: BLE001 - one unreadable PDF must not sink the run
            prev = state["failures"].get(r.case_id, {})
            fails = (prev.get("count", 0) if isinstance(prev, dict) else prev) + 1
            state["failures"][r.case_id] = {"count": fails, "at": stamp,
                                            "error": f"scope check: {type(exc).__name__}: {exc}"[:500]}
            print(f"  scope check failed for {r.name} ({fails}/{MAX_FAILURES_PER_CASE}): {exc}",
                  file=sys.stderr)
            if fails >= MAX_FAILURES_PER_CASE:
                state["cases"][r.case_id] = {"processed_at": stamp, "status": "scope_check_failed",
                                             "docket": r.docket, "name": r.name, "court": court.key}
                state["failures"].pop(r.case_id, None)
            continue
        if ok:
            kept.append(r)
        else:
            state["cases"][r.case_id] = {"processed_at": stamp, "status": "out_of_scope",
                                         "docket": r.docket, "name": r.name, "court": court.key}
    if rows:
        print(f"{court.label}: {len(kept)} of {len(rows)} new rows in scope.")
    return kept


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
    run_time_budget = env_int("RUN_TIME_BUDGET_MINUTES", 40) * 60
    started = time.monotonic()
    backfill = env_int("BACKFILL", 0)

    state = load_json(PROCESSED_PATH, {"page_hash": None, "cases": {}})
    state.setdefault("cases", {})
    state.setdefault("failures", {})
    accum = load_json(ACCUMULATION_PATH, {"ny_date": None, "entries": []})
    accum.setdefault("entries", [])

    # One fingerprint per court; the single pre-2026-10-22 hash was Chancery's.
    hashes = state.setdefault("page_hashes", {})
    if "page_hash" in state:
        old = state.pop("page_hash")
        if old and "chancery" not in hashes:
            hashes["chancery"] = old

    session = http_session()
    retries, wait = env_int("LISTING_RETRIES", 2), env_int("LISTING_RETRY_WAIT", 60)
    listings: dict[str, list[CaseRow]] = {}
    failed_listings = []
    for court in COURTS:
        rows = fetch_listing(session, retries, wait, court)
        if rows:
            listings[court.key] = rows
        else:
            failed_listings.append(court.label)
            # Still empty after re-fetching: most likely the layout changed.
            # The other courts still run; the run exits 2 below so it goes red
            # and GitHub e-mails you, rather than silently recording nothing.
            print(f"ERROR: no opinion rows parsed from the {court.label} listing page.",
                  file=sys.stderr)
    if not listings:
        return 2
    exit_code = 2 if failed_listings else 0

    # A row listed by two courts' pages belongs to the first court that lists it.
    seen: set[str] = set()
    for court in COURTS:
        listings[court.key] = [r for r in listings.get(court.key, [])
                               if not (r.case_id in seen or seen.add(r.case_id))]
    fps = {key: fingerprint(rows) for key, rows in listings.items()}
    all_rows = [r for rows in listings.values() for r in rows]
    unseen = [r for r in all_rows if r.case_id not in state["cases"]]

    # Fast path: identical tables and nothing left over from a capped run.
    if all(fps[k] == hashes.get(k) for k in fps) and not unseen:
        print(f"[{now_ny():%Y-%m-%d %H:%M %Z}] No change ({len(all_rows)} rows). Exiting.")
        set_github_output("changed", "false")
        return exit_code

    stamp = now_ny().isoformat(timespec="seconds")
    max_age = env_int("MAX_AGE_BUSINESS_DAYS", 2)
    new_rows: list[CaseRow] = []
    for court in COURTS:
        rows = listings.get(court.key)
        if not rows:
            continue
        fresh = [r for r in rows if r.case_id not in state["cases"]]
        # First run for this court: seed state instead of summarizing the back
        # catalogue. A court is new until it has a fingerprint or a case in
        # state (cases recorded before courts were tracked are Chancery's).
        known = court.key in hashes or any(
            v.get("court", "chancery") == court.key for v in state["cases"].values())
        if not known:
            seed = fresh[backfill:] if backfill else fresh
            for r in seed:
                state["cases"][r.case_id] = {"processed_at": stamp, "status": "seeded",
                                             "docket": r.docket, "name": r.name,
                                             "court": court.key}
            fresh = fresh[:backfill]
            print(f"{court.label}: seeded {len(seed)} existing rows; "
                  f"backfilling {len(fresh)}.")
            # An explicit backfill on the first run is honoured whatever the dates.
            fresh = check_scope(fresh, court, state, stamp, session)
        else:
            fresh = skip_late_postings(fresh, state, stamp, max_age, now_ny().date())
            fresh = check_scope(fresh, court, state, stamp, session)
        new_rows += fresh
    save_json(PROCESSED_PATH, state)

    import anthropic  # imported lazily: fast path needs no SDK
    from summarize import summarize_opinion

    # Account-level problems (bad/missing key, no credit, no model access) are
    # not the opinion's fault: stop the run red without spending any retries.
    fatal = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
    if new_rows and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        save_json(PROCESSED_PATH, state)
        set_github_output("changed", "true")
        print("ERROR: ANTHROPIC_API_KEY is not set; nothing summarized.", file=sys.stderr)
        return 3

    tokens_used = 0
    processed_now = 0
    for row in new_rows[:max_cases]:
        elapsed = time.monotonic() - started
        if elapsed >= run_time_budget:
            # One opinion (summary + fact check at high effort) can take several
            # minutes; stopping here keeps the run inside the workflow's time
            # limit so the state commit always happens. The rest are picked up
            # next run, as with the token budget.
            print(f"Run time budget reached ({elapsed / 60:.1f} min); deferring the rest.")
            break
        if tokens_used >= run_token_budget:
            print(f"Run token budget reached ({tokens_used}); deferring the rest.")
            break

        # Crash-recovery: summary already accumulated but state not saved.
        if any(e["case_id"] == row.case_id for e in accum["entries"]):
            state["cases"][row.case_id] = {"processed_at": stamp, "status": "ok",
                                           "docket": row.docket, "name": row.name,
                                           "court": row.court}
            save_json(PROCESSED_PATH, state)
            continue

        print(f"Processing {row.case_id}: {row.name} ({row.docket})")
        entry = {**asdict(row), "ny_processed_at": stamp, "ny_date": today_ny()}
        try:
            text, used, note = opinion_text(session, row.url)
            tokens_used += used
            result = summarize_opinion(text, row)
            tokens_used += result.pop("_input_tokens", 0)
            add_note(result, note)
            entry["summary"] = result
            status = "ok"
        except fatal as exc:
            save_json(PROCESSED_PATH, state)
            set_github_output("changed", "true")
            print(f"ERROR: Anthropic rejected the credentials: {exc}", file=sys.stderr)
            return 3
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
                                       "docket": row.docket, "name": row.name,
                                       "court": row.court}
        state["failures"].pop(row.case_id, None)
        save_json(PROCESSED_PATH, state)
        processed_now += 1

    backlog = [r for r in all_rows if r.case_id not in state["cases"]]
    for key, rows in listings.items():
        if not any(r.case_id not in state["cases"] for r in rows):
            hashes[key] = fps[key]  # only now is the fast path allowed to skip this page
    state["cases"] = _prune(state["cases"])
    state["last_checked_ny"] = stamp
    save_json(PROCESSED_PATH, state)

    print(f"Processed {processed_now}; backlog {len(backlog)}; input tokens {tokens_used}.")
    set_github_output("changed", "true")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
