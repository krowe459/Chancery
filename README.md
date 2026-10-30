# Delaware Business Courts Digest

Diff-driven watcher for Delaware business-court opinions. It summarizes each new
opinion with Claude and emails one HTML digest per New York business day through Resend.

Courts watched (`COURTS` in `src/scrape.py`):
- **Court of Chancery**: every opinion and order on its listing.
- **Supreme Court of Delaware**: decisions on appeal from the Court of Chancery. Each new
  row's PDF caption is read for "Court Below: Court of Chancery" (or a Chancery C.A. No.).
- **Superior Court, CCLD**: Complex Commercial Litigation Division decisions, found by
  "CCLD" in the listing row's case number, or failing that in the PDF caption.

Rows outside scope are recorded in `state/processed_cases.json` as `out_of_scope` and never
summarized or re-read. In the digest, Supreme Court entries come first, then Chancery, then
CCLD, each non-Chancery entry with a court label.

```
scrape.yml (every 2h, weekdays) ──► src/scrape.py ──► state/processed_cases.json
          │ unchanged? exit (≈1 billed minute)        state/daily_accumulation.json
          └─ new rows ─► src/summarize.py (single pass ≤150k tokens, else map→reduce)
digest.yml (≈6pm New York)       ──► last-minute scrape ──► src/send_digest.py ──► Resend ──► inbox
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
3. Optional **Variables**: `CLAUDE_MODEL` (default `claude-opus-5-5`), `VERIFY_MODEL`
   (default `claude-sonnet-5-5`), `VERIFY_FACTS` (`0` turns the fact check off), `MAX_CASES_PER_RUN` (8),
   `RUN_TOKEN_BUDGET` (400000), `RUN_TIME_BUDGET_MINUTES` (40; later opinions wait for the next run),
   `MAX_AGE_BUSINESS_DAYS` (2; opinions the court lists more than
   this many business days after their decision date are skipped, `-1` keeps them), `DIGEST_FROM`.
4. **Settings → Actions → General → Workflow permissions → Read and write**.
5. Run **chancery-scrape** by hand once (Actions tab → Run workflow). The first run *seeds*
   the opinions already listed, so you aren't billed for the back catalogue. Set
   `backfill: 5` to summarize the 5 newest instead.
6. Test delivery: run **chancery-digest** with `force: true`.
7. Set a monthly spend limit in the Anthropic Console as a billing backstop.

## Previewing summaries
Run **chancery-preview** (Actions tab → Run workflow) with up to 5 opinion URLs or download ids
(e.g. `402160`). It runs the full pipeline on the current code and attaches the rendered digest
and full JSON as the `chancery-preview` artifact; the run's summary page lists any fact-check
flags. It never writes state or sends email, so use it to test prompt changes or to
re-summarize an opinion that was already processed (`backfill` only works on the first run).
Locally: `cd src && python preview.py 402160` writes `preview/`.

## Operational notes
- Just before sending, the digest workflow runs one more scrape (capped at 12 minutes,
  `PREDIGEST_TIME_BUDGET_MINUTES`) so opinions posted after the 5:45pm scrape make that
  evening's email. It only runs when a digest is actually due, and a failure there never
  blocks the email.
- A court watched for the first time is seeded: the rows already on its listing are
  recorded, not summarized, so adding a court never bills for its back catalogue.
- If a listing page parses to no opinions, `scrape.py` waits and re-fetches it
  (`LISTING_RETRIES`, default 2, `LISTING_RETRY_WAIT`, default 60 seconds), logging each empty
  page's status and title. If it is still empty (e.g. the court redesigned the page), the other courts still run
  and the scrape then exits with code 2. The run goes red and
  GitHub emails you. The parser finds columns by header text, so reordered columns still work.
- GitHub disables scheduled workflows in public repos after 60 days with no repository
  activity. The daily state commits count as activity.
- PDFs are read with `src/pdftext.py`, which separates footnotes from body text by font size
  and places each footnote, as `[^n]: ...`, after the paragraph that cites it. If an opinion's
  layout doesn't fit (or text would be lost), it falls back to plain pypdf extraction.
- A PDF with no text layer (an image scan) is sent to Claude as a document, which transcribes
  it from the page images; the transcript then goes through the normal summary and checks,
  and the digest notes that the summary rests on a transcription. This costs one extra request
  (counted toward `RUN_TOKEN_BUDGET`) and is limited to PDFs under about 23 MB and 600 pages.
- The final summary call includes one worked example (`src/prompt_example.json`) to set the
  standard. Edit that file to change it, and keep it accurate to its opinion. It is skipped
  when summarizing that same opinion. System prompts are cached across a run's calls.
- After each summary, a second model (`VERIFY_MODEL`) checks its key facts, rejected
  arguments, holding and brief summary against the opinion text (`src/verify.py`), and what the
  practical takeaway says about the case and the Court's reasoning, including any advice the
  Court's reasoning shows would not help (it does not judge whether advice is wise). Statements
  it finds unsupported or contradicted are flagged in the digest. For opinions too long for one
  request it checks chunk by chunk and flags only contradictions. Its tokens count toward
  `RUN_TOKEN_BUDGET`.
- The digest shows at most 8 key facts, 10 rejected arguments and 12 authorities per opinion,
  plus any flagged item, with a link to the full summary in `state/archive/`.
- Every cited authority is checked against the opinion text (reporter cite, first party
  name, section or rule number). Misses are flagged in the digest. A miss is a reason to
  look, not proof the citation was invented. Tests: `python -m unittest discover -s tests`.
- Summaries are AI-generated triage aids. Verify them against the opinion before relying on them.
