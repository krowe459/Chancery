# Chancery Digest

Diff-driven watcher for Delaware Court of Chancery opinions. It summarizes each new
opinion with Claude and emails one HTML digest per New York business day through Resend.

```
scrape.yml (every 2h, weekdays) ──► src/scrape.py ──► state/processed_cases.json
          │ unchanged? exit (≈1 billed minute)        state/daily_accumulation.json
          └─ new rows ─► src/summarize.py (≤30k-token requests, map→reduce)
digest.yml (≈6pm New York)       ──► src/send_digest.py ──► Resend ──► inbox
                                     archive → state/archive/YYYY-MM-DD.json
```

## Setup (about 10 minutes)

1. Create a GitHub repo. A **public** repo gets unlimited free Actions minutes;
   a private one uses about 200 of its 2,000 free minutes a month. Push this folder to `main`.
2. **Settings → Secrets and variables → Actions → Secrets**:
   - `ANTHROPIC_API_KEY`
   - `RESEND_API_KEY`: from resend.com (free tier: 3,000 emails/month, 100/day)
   - `DIGEST_TO`: your inbox. With the default sender `onboarding@resend.dev`, Resend
     only delivers to the address you signed up with. Verify a domain to send elsewhere.
3. Optional **Variables**: `CLAUDE_MODEL` (default `claude-opus-5-5`), `MAX_CASES_PER_RUN` (8),
   `RUN_TOKEN_BUDGET` (400000), `DIGEST_FROM`.
4. **Settings → Actions → General → Workflow permissions → Read and write**.
5. Run **chancery-scrape** by hand once (Actions tab → Run workflow). The first run *seeds*
   the opinions already listed, so you aren't billed for the back catalogue. Set
   `backfill: 5` to summarize the 5 newest instead.
6. Test delivery: run **chancery-digest** with `force: true`.
7. Set a monthly spend limit in the Anthropic Console as a billing backstop.

## Operational notes
- If the court redesigns its page, `scrape.py` exits with code 2. The run goes red and
  GitHub emails you. The parser finds columns by header text, so reordered columns still work.
- GitHub disables scheduled workflows in public repos after 60 days with no repository
  activity. The daily state commits count as activity.
- PDFs are read with `src/pdftext.py`, which separates footnotes from body text by font size
  and places each footnote, as `[^n]: ...`, after the paragraph that cites it. If an opinion's
  layout doesn't fit (or text would be lost), it falls back to plain pypdf extraction.
- Every cited authority is checked against the opinion text (reporter cite, first party
  name, section or rule number). Misses are flagged in the digest. A miss is a reason to
  look, not proof the citation was invented. Tests: `python -m unittest discover -s tests`.
- Summaries are AI-generated triage aids. Verify them against the opinion before relying on them.
