"""Preview summaries for chosen opinions without touching state or sending email.

Runs the same pipeline as a scrape (extraction, summary, citation check, fact
check) on the opinions you name, and writes:

  preview/digest-preview.html   the entries rendered exactly as the digest would
  preview/summaries.json        the full results, including fact_check flags

Nothing under state/ is read for writing, nothing is committed, and no email
is sent. Use it to see the effect of a prompt or pipeline change, or to
re-summarize an opinion that has already been processed (BACKFILL only works
on the first run).

Usage:  python src/preview.py <opinion> [<opinion> ...]
        PREVIEW_OPINIONS="<opinion> <opinion>" python src/preview.py
An <opinion> is a courts.delaware.gov opinion URL or its numeric download id
(e.g. 402160 for .../opinions/download.aspx?id=402160).

Env:    ANTHROPIC_API_KEY (required), CLAUDE_MODEL, VERIFY_MODEL, VERIFY_FACTS,
        PREVIEW_MAX (default 5): maximum opinions per run, as a cost ceiling.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common import ACCUMULATION_PATH, ARCHIVE_DIR, ROOT, env_int, load_json, today_ny
from scrape import COURTS, CaseRow, extract_text, get_with_retry, http_session, parse_listing

OUT_DIR = ROOT / "preview"
DOWNLOAD_URL = "https://courts.delaware.gov/opinions/download.aspx?id={id}"
ALLOWED_HOSTS = {"courts.delaware.gov", "www.courts.delaware.gov"}


def normalize(opinion: str) -> str:
    """A download id or a courts.delaware.gov URL -> canonical https URL."""
    opinion = opinion.strip()
    if re.fullmatch(r"\d{3,9}", opinion):
        return DOWNLOAD_URL.format(id=opinion)
    parsed = urlparse(opinion)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Not a courts.delaware.gov https URL or download id: {opinion!r}")
    ids = parse_qs(parsed.query).get("id")
    return DOWNLOAD_URL.format(id=ids[0]) if ids and ids[0].isdigit() else opinion


def _key(url: str) -> str:
    ids = parse_qs(urlparse(url).query).get("id")
    return ids[0] if ids else url


def known_rows(session) -> dict[str, dict]:
    """Case metadata by download id: past archives and the accumulation first,
    then the courts' current listings (which win, being freshest)."""
    rows: dict[str, dict] = {}
    for path in sorted(ARCHIVE_DIR.glob("*.json")) + [ACCUMULATION_PATH]:
        for e in load_json(path, {}).get("entries", []):
            if e.get("url"):
                rows[_key(e["url"])] = {f: e.get(f, "chancery" if f == "court" else "")
                                        for f in CaseRow.__dataclass_fields__}
    for court in reversed(COURTS):   # Chancery last, so it wins a shared row
        try:
            html = get_with_retry(session, court.listing_url).text
            for r in parse_listing(html, court.listing_url, court.key):
                rows[_key(r.url)] = asdict(r)
        except Exception as exc:  # noqa: BLE001 - metadata is a nicety; carry on without it
            print(f"Warning: could not read the {court.label} listing ({exc}); "
                  "using archived metadata.")
    return rows


def preview(opinions: list[str]) -> int:
    from send_digest import render_html
    from summarize import summarize_opinion

    limit = env_int("PREVIEW_MAX", 5)
    urls = list(dict.fromkeys(normalize(o) for o in opinions))
    if not urls:
        print("No opinions given.", file=sys.stderr)
        return 2
    if len(urls) > limit:
        print(f"{len(urls)} opinions given; PREVIEW_MAX is {limit}.", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 3

    session = http_session()
    meta = known_rows(session)
    entries, tokens = [], 0
    for url in urls:
        info = meta.get(_key(url)) or {"case_id": f"preview-{_key(url)}", "name": url,
                                       "docket": "", "date": "", "judge": "",
                                       "opinion_type": "", "url": url, "court": ""}
        row = CaseRow(**{**info, "url": url})
        print(f"Summarizing {row.name} ({row.docket or 'docket unknown'})")
        entry = {**asdict(row), "ny_date": today_ny()}
        try:
            result = summarize_opinion(extract_text(session, url), row)
            tokens += result.pop("_input_tokens", 0)
            entry["summary"] = result
        except Exception as exc:  # noqa: BLE001 - show the failure in the preview
            entry["error"] = f"Summary failed: {type(exc).__name__}: {exc}"[:500]
            print(f"  failed: {entry['error']}", file=sys.stderr)
        entries.append(entry)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "summaries.json").write_text(
        json.dumps({"generated": today_ny(), "input_tokens": tokens, "entries": entries},
                   indent=2, ensure_ascii=False) + "\n", "utf-8")
    (OUT_DIR / "digest-preview.html").write_text(
        render_html(entries, today_ny(), full_summary_ref="summaries.json in this preview",
                    title="Delaware Business Courts Digest &mdash; Preview (not sent)"), "utf-8")

    report = [f"Previewed {len(entries)} opinion(s); {tokens:,} input tokens sent.", ""]
    for e in entries:
        s = e.get("summary") or {}
        fc = s.get("fact_check") or {}
        cites = (s.get("citation_check") or {}).get("not_found", 0)
        status = ("FAILED: " + e["error"]) if e.get("error") else (
            f"{len(fc.get('flags') or [])} fact-check flag(s)"
            + (f" ({fc['coverage']})" if fc.get("coverage") else "")
            + ("; fact check did not run" if fc.get("error") else "")
            + f"; {cites} citation(s) not found in the opinion")
        report.append(f"- {e['name']}: {status}")
        for f in fc.get("flags") or []:
            report.append(f"  - {f['field']}"
                          + (f" #{f['index'] + 1}" if f.get("index") is not None else "")
                          + f" ({f['verdict']}): {f['problem']}")
    print("\n".join(report))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Chancery preview\n\n" + "\n".join(report) + "\n\n"
                     "Download the **chancery-preview** artifact for the rendered digest "
                     "and full JSON.\n")
    return 0 if all("summary" in e for e in entries) else 1


if __name__ == "__main__":
    args = sys.argv[1:] or os.environ.get("PREVIEW_OPINIONS", "").replace(",", " ").split()
    try:
        sys.exit(preview(args))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
