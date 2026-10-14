"""Compile the accumulated summaries into an HTML digest and send it via Resend.

Exactly-once per New York calendar day, regardless of which UTC cron fired:
  * gate:   only sends on a NY weekday at/after DIGEST_HOUR_NY local time;
  * ledger: sent_digests.json records each NY date already sent;
  * API:    Resend Idempotency-Key "chancery-digest/<NY date>" dedupes even if
            the ledger commit is lost after a successful send.
An empty accumulation file exits 0 without sending anything.

Usage:  python src/send_digest.py [--force]
Env:    RESEND_API_KEY, DIGEST_TO
        DIGEST_FROM     (default "Chancery Digest <onboarding@resend.dev>")
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
DEFAULT_FROM = "Chancery Digest <onboarding@resend.dev>"


def _e(value: object) -> str:
    """Escape everything: court metadata and model output are untrusted."""
    return html.escape(str(value or ""), quote=True)


def render_entry(entry: dict) -> str:
    s = entry.get("summary") or {}
    meta = " &middot; ".join(
        _e(x) for x in (entry.get("docket"), entry.get("date"), entry.get("judge"),
                        entry.get("opinion_type")) if x
    )
    head = f"""
      <tr><td style="padding:20px 24px 0 24px;">
        <div style="font-size:17px;font-weight:700;color:#0b2545;line-height:1.3;">
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

    auths = "".join(
        f"<li style='margin:0 0 4px 0;'><strong>{_e(a.get('authority'))}</strong> &mdash; "
        f"{_e(a.get('proposition'))}</li>"
        for a in s.get("authorities_cited", [])
    ) or "<li>None identified.</li>"
    note = (f"<div style='font-size:12px;color:#8a6d1c;margin-top:8px;'>&#9888; "
            f"{_e(s['coverage_note'])}</div>") if s.get("coverage_note") else ""

    def section(label: str, content: str) -> str:
        return (f"<div style='margin-top:12px;'><div style='font-size:11px;letter-spacing:.06em;"
                f"text-transform:uppercase;color:#5c6b7a;font-weight:700;'>{label}</div>"
                f"<div style='font-size:14px;color:#1d2731;line-height:1.5;margin-top:3px;'>"
                f"{content}</div></div>")

    body = f"""
      <tr><td style="padding:0 24px 20px 24px;">
        {section("Core Legal Issue", _e(s.get("core_legal_issue")))}
        {section("DGCL / Common-Law Authorities",
                 f"<ul style='margin:4px 0 0 18px;padding:0;'>{auths}</ul>")}
        {section("Holding", _e(s.get("holding")))}
        {section("Brief Summary", _e(s.get("brief_summary")))}
        {note}
      </td></tr>"""
    return head + body + _divider()


def _divider() -> str:
    return "<tr><td style='padding:0 24px;'><hr style='border:0;border-top:1px solid #e3e8ee;margin:0;'></td></tr>"


def render_html(entries: list[dict], ny_date: str) -> str:
    rows = "".join(render_entry(e) for e in entries)
    n = len(entries)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f5f8;font-family:Georgia,'Times New Roman',serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f8;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:680px;background:#ffffff;border:1px solid #e3e8ee;border-radius:6px;">
        <tr><td style="background:#0b2545;padding:20px 24px;border-radius:6px 6px 0 0;">
          <div style="color:#ffffff;font-size:20px;font-weight:700;">Delaware Court of Chancery &mdash; Daily Digest</div>
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore the NY weekday/hour gate")
    args = parser.parse_args()

    now = now_ny()
    ny_date = today_ny()
    sent = load_json(SENT_PATH, {"sent": []})
    set_github_output("sent", "false")

    if any(s["ny_date"] == ny_date for s in sent["sent"]):
        print(f"Digest for {ny_date} already sent. Exiting.")
        return 0
    if not args.force:
        if now.weekday() >= 5:
            print(f"{ny_date} is a weekend in New York. Exiting.")
            return 0
        if now.hour < env_int("DIGEST_HOUR_NY", 18):
            print(f"Too early ({now:%H:%M %Z}). This is the off-DST cron slot; exiting.")
            return 0

    accum = load_json(ACCUMULATION_PATH, {"ny_date": None, "entries": []})
    entries = accum.get("entries", [])
    if not entries:
        print("Accumulation file is empty. No email sent.")
        return 0

    entries = sorted(entries, key=lambda e: (e.get("date", ""), e.get("name", "")), reverse=True)
    n = len(entries)
    subject = f"Chancery Digest — {now:%a %b %-d, %Y} — {n} new decision{'s' if n != 1 else ''}"
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
