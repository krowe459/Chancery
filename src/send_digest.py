"""Compile the accumulated summaries into an HTML digest and send it via Resend.

Exactly-once per New York calendar day, regardless of which UTC cron fired:
  * gate:   only sends on a NY weekday at/after DIGEST_HOUR_NY local time;
  * ledger: sent_digests.json records each NY date already sent;
  * API:    Resend Idempotency-Key "chancery-digest/<NY date>" dedupes even if
            the ledger commit is lost after a successful send.
An empty accumulation file exits 0 without sending anything.

Usage:  python src/send_digest.py [--force] [--check]
        --check reports whether a digest is due (GitHub output "due") without
        sending anything; the workflow uses it to decide whether to run a
        last-minute scrape first.
Env:    RESEND_API_KEY, DIGEST_TO
        DIGEST_FROM     (default "Delaware Business Courts Digest <onboarding@resend.dev>")
        DIGEST_HOUR_NY  (default 18)
"""
from __future__ import annotations

import argparse
import html
import os
import sys

import requests

from common import (
    ACCUMULATION_PATH,
    ARCHIVE_DIR,
    SENT_PATH,
    env_int,
    load_json,
    now_ny,
    save_json,
    set_github_output,
    today_ny,
)

RESEND_URL = "https://api.resend.com/emails"
# Display caps per list. The full summary stays in state/archive/<date>.json.
MAX_KEY_FACTS = 8
MAX_ARGUMENTS = 10
MAX_AUTHORITIES = 12
DEFAULT_FROM = "Delaware Business Courts Digest <onboarding@resend.dev>"
# Court label on each entry, and the digest's order: appellate rulings on
# Chancery first, then Chancery, then the Superior Court's CCLD. Entries from
# before courts were tracked have no "court" and are Chancery's.
COURT_LABELS = {"supreme": "Supreme Court (appeal from Chancery)",
                "chancery": "Court of Chancery",
                "superior": "Superior Court CCLD"}
COURT_ORDER = list(COURT_LABELS)


def order_entries(entries: list[dict]) -> list[dict]:
    newest = sorted(entries, key=lambda e: (e.get("date", ""), e.get("name", "")), reverse=True)
    rank = lambda e: (COURT_ORDER.index(e.get("court") or "chancery")
                      if (e.get("court") or "chancery") in COURT_ORDER else len(COURT_ORDER))
    return sorted(newest, key=rank)


def _e(value: object) -> str:
    """Escape everything: court metadata and model output are untrusted."""
    return html.escape(str(value or ""), quote=True)


def _archive_link(ny_date: str) -> str:
    """Where the full summary lives once the digest's state commit lands."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    path = f"state/archive/{ny_date}.json"
    if not repo:
        return _e(path)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    branch = os.environ.get("GITHUB_REF_NAME", "").strip() or "main"
    url = f"{server}/{repo}/blob/{branch}/{path}"
    return f"<a href='{_e(url)}' style='color:#5c6b7a;'>{_e(path)}</a>"


def _capped(items: list, cap: int, keep: set[int]) -> tuple[list[tuple[int, object]], int]:
    """First `cap` items plus any beyond it in `keep` (flagged items stay
    visible); returns the (index, item) pairs shown and how many were hidden."""
    shown = [(i, x) for i, x in enumerate(items) if i < cap or i in keep]
    return shown, len(items) - len(shown)


def render_entry(entry: dict, ny_date: str = "", full_summary_ref: str | None = None) -> str:
    s = entry.get("summary") or {}
    court = entry.get("court") or "chancery"
    label = "" if court == "chancery" else COURT_LABELS.get(court, court)
    meta = " &middot; ".join(
        _e(x) for x in (entry.get("docket"), entry.get("date"), entry.get("judge"),
                        entry.get("opinion_type")) if x
    )
    tag = (f"<div style='font-size:11px;font-weight:700;letter-spacing:.04em;"
           f"text-transform:uppercase;color:#8a5a00;margin-bottom:4px;'>{_e(label)}</div>"
           if label else "")
    head = f"""
      <tr><td style="padding:20px 24px 0 24px;">
        {tag}<div style="font-size:17px;font-weight:700;color:#0b2545;line-height:1.3;">
          <a href="{_e(entry.get('url'))}" style="color:#0b2545;text-decoration:none;">{_e(entry.get('name'))}</a>
        </div>
        <div style="font-size:12px;color:#5c6b7a;margin-top:4px;">{meta}</div>
      </td></tr>"""

    if entry.get("error"):
        body = f"""
      <tr><td style="padding:10px 24px 20px 24px;font-size:14px;color:#8a1c1c;">
        {_e(entry['error'])} <a href="{_e(entry.get('url'))}">Read the opinion</a>.
      </td></tr>"""
        return head + body + _divider()

    # Entries summarized before the schema gained "role" have none; show them all.
    authorities = s.get("authorities_cited", [])
    main_auths = [a for a in authorities if a.get("role", "relied_on") != "background"]
    background = [a for a in authorities if a.get("role") == "background"]

    def flag_marks(field: str) -> dict:
        """index (None for text fields) -> warning HTML for flagged claims."""
        marks: dict = {}
        for f in flags:
            if f.get("field") == field:
                said = (f" &ldquo;{_e(f['assertion'])}&rdquo; &mdash; opinion: "
                        f"{_e(f.get('opinion_says'))}." if f.get("assertion") else "")
                marks[f.get("index")] = (
                    f"<div style='color:#8a1c1c;font-size:12px;margin-top:2px;'>&#9888; "
                    f"Fact check ({_e(f.get('verdict'))}):{said} {_e(f.get('problem'))}</div>")
        return marks

    def more_line(n: int) -> str:
        where = (_e(full_summary_ref) if full_summary_ref is not None
                 else _archive_link(ny_date or str(entry.get("ny_date") or "")))
        return (f"<li style='margin:0 0 4px 0;color:#5c6b7a;list-style:none;margin-left:-18px;"
                f"font-size:12px;'>+{n} more in the full summary ({where})</li>")

    def unverified(a: dict) -> str:
        if a.get("check") != "not_found":
            return ""
        return (f" <span style='color:#8a1c1c;font-size:12px;'>&#9888; not found in opinion "
                f"text ({_e(a.get('check_detail'))})</span>")

    def auth_item(a: dict) -> str:
        tag = " <em>(distinguished)</em>" if a.get("role") == "distinguished" else ""
        return (f"<li style='margin:0 0 4px 0;'><strong>{_e(a.get('authority'))}</strong>{tag} "
                f"&mdash; {_e(a.get('proposition'))}{unverified(a)}</li>")

    # Flagged and distinguished authorities stay visible past the cap: the
    # cases an opinion distinguishes are often the ones it turns on (Khanna
    # was hidden as the 13th authority in the Oct. 20 Glass v. RWA entry).
    shown_auths, hidden_auths = _capped(
        main_auths, MAX_AUTHORITIES,
        {i for i, a in enumerate(main_auths)
         if a.get("check") == "not_found" or a.get("role") == "distinguished"})
    auths = "".join(auth_item(a) for _, a in shown_auths) or "<li>None identified.</li>"
    if hidden_auths:
        auths += more_line(hidden_auths)
    if background:
        names = "; ".join(_e(a.get("authority")) + unverified(a) for a in background)
        auths += (f"<li style='margin:0 0 4px 0;color:#5c6b7a;list-style:none;margin-left:-18px;'>"
                  f"Also cited: {names}</li>")
    note = (f"<div style='font-size:12px;color:#8a6d1c;margin-top:8px;'>&#9888; "
            f"{_e(s['coverage_note'])}</div>") if s.get("coverage_note") else ""
    fc = s.get("fact_check") or {}
    flags = fc.get("flags") or []
    if flags:
        what = "contradicted by" if fc.get("coverage") == "contradictions_only" else "not supported by"
        note += (f"<div style='font-size:12px;color:#8a1c1c;margin-top:8px;'>&#9888; "
                 f"{len(flags)} statement{'s' if len(flags) != 1 else ''} in this summary "
                 f"{'were' if len(flags) != 1 else 'was'} flagged as {what} the opinion text "
                 f"(marked above). Check them before relying on this summary.</div>")
    elif fc.get("error"):
        note += ("<div style='font-size:12px;color:#8a6d1c;margin-top:8px;'>&#9888; "
                 "The fact check did not run for this summary.</div>")
    misses = (s.get("citation_check") or {}).get("not_found", 0)
    if misses:
        note += (f"<div style='font-size:12px;color:#8a1c1c;margin-top:8px;'>&#9888; "
                 f"{misses} cited authorit{'y was' if misses == 1 else 'ies were'} not found "
                 f"in the opinion text. Check them before relying on this summary.</div>")

    def section(label: str, content: str) -> str:
        return (f"<div style='margin-top:12px;'><div style='font-size:11px;letter-spacing:.06em;"
                f"text-transform:uppercase;color:#5c6b7a;font-weight:700;'>{label}</div>"
                f"<div style='font-size:14px;color:#1d2731;line-height:1.5;margin-top:3px;'>"
                f"{content}</div></div>")

    def optional_list(label: str, key: str, cap: int) -> str:
        items = s.get(key) or []
        if not items:
            return ""
        marks = flag_marks(key)
        shown, hidden = _capped(items, cap, set(marks))
        lis = "".join(f"<li style='margin:0 0 4px 0;'>{_e(x)}{marks.get(i, '')}</li>"
                      for i, x in shown)
        if hidden:
            lis += more_line(hidden)
        return section(label, f"<ul style='margin:4px 0 0 18px;padding:0;'>{lis}</ul>")

    def optional_text(label: str, key: str) -> str:
        return section(label, _e(s[key])) if s.get(key) else ""

    def text_with_flags(key: str) -> str:
        return _e(s.get(key)) + "".join(flag_marks(key).values())

    disposition = " &middot; ".join(
        f"{label}: {_e(s[key])}" for label, key in
        (("Disposition", "disposition"), ("Standard", "standard_of_review")) if s.get(key)
    )
    disposition_html = (f"<div style='font-size:13px;color:#1d2731;margin-top:12px;'>"
                        f"{disposition}</div>") if disposition else ""

    body = f"""
      <tr><td style="padding:0 24px 20px 24px;">
        {section("Core Legal Issue", _e(s.get("core_legal_issue")))}
        {disposition_html}
        {optional_list("Key Facts &amp; Concessions", "key_facts_and_concessions", MAX_KEY_FACTS)}
        {optional_list("Arguments Rejected or Waived", "arguments_rejected_or_waived", MAX_ARGUMENTS)}
        {section("Authorities Cited",
                 f"<ul style='margin:4px 0 0 18px;padding:0;'>{auths}</ul>")}
        {section("Holding", text_with_flags("holding"))}
        {section("Brief Summary", text_with_flags("brief_summary"))}
        {optional_text("Practical Takeaway (inference, not holding)", "practical_takeaway")}
        {note}
      </td></tr>"""
    return head + body + _divider()


def _divider() -> str:
    return "<tr><td style='padding:0 24px;'><hr style='border:0;border-top:1px solid #e3e8ee;margin:0;'></td></tr>"


def render_html(entries: list[dict], ny_date: str, full_summary_ref: str | None = None,
                title: str = "Delaware Business Courts &mdash; Daily Digest") -> str:
    """full_summary_ref overrides where "+N more" points (default: the archive);
    title is trusted HTML."""
    rows = "".join(render_entry(e, ny_date, full_summary_ref) for e in entries)
    n = len(entries)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f5f8;font-family:Georgia,'Times New Roman',serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f8;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:680px;background:#ffffff;border:1px solid #e3e8ee;border-radius:6px;">
        <tr><td style="background:#0b2545;padding:20px 24px;border-radius:6px 6px 0 0;">
          <div style="color:#ffffff;font-size:20px;font-weight:700;">{title}</div>
          <div style="color:#c9d6e3;font-size:13px;margin-top:4px;">{_e(ny_date)} &middot; {n} new decision{'s' if n != 1 else ''}</div>
        </td></tr>
        {rows}
        <tr><td style="padding:16px 24px;font-size:11px;color:#8795a1;font-family:Arial,sans-serif;">
          AI-generated summaries for internal triage only; not legal advice. Verify against the
          official opinion before relying on any statement. Source: courts.delaware.gov.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_via_resend(subject: str, html_body: str, idempotency_key: str) -> str:
    api_key = os.environ["RESEND_API_KEY"]
    to = [a.strip() for a in os.environ["DIGEST_TO"].split(",") if a.strip()]
    resp = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        json={
            "from": os.environ.get("DIGEST_FROM", "").strip() or DEFAULT_FROM,
            "to": to,
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend error {resp.status_code}: {resp.text[:500]}")
    return resp.json().get("id", "")


def digest_due(now, ny_date: str, sent: dict, force: bool) -> tuple[bool, str]:
    """Whether today's digest should go out now, and why not if it shouldn't."""
    if any(s["ny_date"] == ny_date for s in sent["sent"]):
        return False, f"Digest for {ny_date} already sent."
    if not force:
        if now.weekday() >= 5:
            return False, f"{ny_date} is a weekend in New York."
        if now.hour < env_int("DIGEST_HOUR_NY", 18):
            return False, f"Too early ({now:%H:%M %Z}). This is the off-DST cron slot."
    return True, "Digest is due."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore the NY weekday/hour gate")
    parser.add_argument("--check", action="store_true",
                        help="only report whether a digest is due (output 'due'); send nothing")
    args = parser.parse_args()

    now = now_ny()
    ny_date = today_ny()
    sent = load_json(SENT_PATH, {"sent": []})
    set_github_output("sent", "false")

    due, reason = digest_due(now, ny_date, sent, args.force)
    if args.check:
        set_github_output("due", "true" if due else "false")
        print(reason)
        return 0
    if not due:
        print(f"{reason} Exiting.")
        return 0

    accum = load_json(ACCUMULATION_PATH, {"ny_date": None, "entries": []})
    entries = accum.get("entries", [])
    if not entries:
        print("Accumulation file is empty. No email sent.")
        return 0

    entries = order_entries(entries)
    n = len(entries)
    subject = f"Delaware Business Courts Digest — {now:%a %b %-d, %Y} — {n} new decision{'s' if n != 1 else ''}"
    email_id = send_via_resend(subject, render_html(entries, ny_date), f"chancery-digest/{ny_date}")
    print(f"Sent digest {email_id} with {n} entries.")

    # Archive, then clear only what was sent, then record the ledger.
    archive_path = ARCHIVE_DIR / f"{ny_date}.json"
    archived = load_json(archive_path, {"ny_date": ny_date, "entries": []})
    have = {e["case_id"] for e in archived["entries"]}
    archived["entries"] += [e for e in entries if e["case_id"] not in have]
    save_json(archive_path, archived)

    sent_ids = {e["case_id"] for e in entries}
    accum["entries"] = [e for e in accum["entries"] if e["case_id"] not in sent_ids]
    accum["ny_date"] = None
    save_json(ACCUMULATION_PATH, accum)

    sent["sent"].append({"ny_date": ny_date, "email_id": email_id, "count": n,
                         "sent_at_ny": now.isoformat(timespec="seconds")})
    sent["sent"] = sent["sent"][-90:]
    save_json(SENT_PATH, sent)
    set_github_output("sent", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
