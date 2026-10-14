"""Footnote-aware PDF text extraction.

Plain extract_text() reads a page top to bottom, so each page's footnotes land
in the middle of whatever sentence runs onto the next page, and footnote
reference numbers become stray digits ("...LLC.” 3 Wal-Mart.com..."). Court of
Chancery opinions put real rulings in footnotes, so this module rebuilds the
text from word positions and font sizes instead:

  * body text keeps its paragraphs, joined across page breaks;
  * a footnote reference becomes an inline marker: "...LLC.”[^3]";
  * each footnote's text follows the paragraph that cites it:
        [^3]: POB Exs., at 63 ...
    including footnotes that continue onto the next page;
  * running page headers ("C.A. No. ... Page 2 of 26") are dropped.

How the parts are told apart, per page:
  * body size = the most common font size in the document;
  * the footnote area is the run of lines at the bottom of the page that are
    smaller than body text (letterhead or small text above body lines is not);
  * a reference marker is a short number set well below its line's text size.

pdf_to_text() returns None when the layout doesn't fit this model (no text
found, or too much text lost); the caller then falls back to plain extraction.
"""
from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field

LINE_TOLERANCE = 4.0     # pt: words this close vertically share a line (markers sit ~2pt high)
MARKER_RATIO = 0.80      # a number below 80% of its line's text size is a marker
SMALL_RATIO = 0.97       # a line below 97% of body size is footnote-sized
HEADER_BAND = 0.12       # top/bottom fraction of the page searched for running headers
MIN_KEPT_RATIO = 0.90    # fall back if we keep less than 90% of plain extraction's letters


@dataclass
class Line:
    words: list[dict]
    top: float
    x0: float
    x1: float
    size: float                      # dominant text size, ignoring markers
    text: str = ""
    markers: list[tuple[int, int]] = field(default_factory=list)  # (char offset, number)


# ------------------------------------------------------------ line assembly

def _build_lines(words: list[dict]) -> list[list[dict]]:
    """Group words (already in reading order) into visual lines."""
    lines: list[list[dict]] = []
    for w in words:
        if lines:
            ref = [x for x in lines[-1] if x["size"] >= _line_size(lines[-1]) * MARKER_RATIO]
            ref_top = sum(x["top"] for x in ref) / len(ref) if ref else lines[-1][0]["top"]
            if abs(w["top"] - ref_top) <= LINE_TOLERANCE:
                lines[-1].append(w)
                continue
        lines.append([w])
    return lines


def _line_size(words: list[dict]) -> float:
    sizes = Counter()
    for w in words:
        sizes[round(w["size"], 1)] += len(w["text"])
    return sizes.most_common(1)[0][0]


def _merge_small_caps(words: list[dict]) -> list[dict]:
    """Rejoin small caps: "C" + "OURT" (set smaller, right after) -> "COURT"."""
    out: list[dict] = []
    for w in words:
        prev = out[-1] if out else None
        # Only a lone capital can start a small-caps word, and the rest of the
        # word must touch it (a real space is ~0.25em; small caps sit ~0).
        if (prev is not None and re.fullmatch(r"[A-Z]", prev["text"])
                and re.fullmatch(r"[A-Z][A-Z.,'’]*", w["text"])
                and w["size"] < prev["size"] and w["x0"] - prev["x1"] < prev["size"] * 0.12):
            out[-1] = {**prev, "text": prev["text"] + w["text"], "x1": w["x1"]}
            continue
        out.append(w)
    return out


def _make_line(words: list[dict]) -> Line:
    words = _merge_small_caps(words)
    size = _line_size(words)
    parts: list[str] = []
    markers: list[tuple[int, int]] = []
    for w in words:
        text = w["text"]
        if w["size"] < size * MARKER_RATIO and re.fullmatch(r"\d{1,3}", text):
            markers.append((len(" ".join(parts)), int(text)))
            continue
        parts.append(text)
    body = [w for w in words if w["size"] >= size * MARKER_RATIO] or words
    return Line(words=words, top=min(w["top"] for w in body),
                x0=min(w["x0"] for w in words), x1=max(w["x1"] for w in words),
                size=size, text=" ".join(parts), markers=markers)


def _header_key(text: str) -> str:
    return re.sub(r"\d+", "#", text).strip().lower()


def _running_headers(pages: list[tuple[float, list[Line]]]) -> set[str]:
    """Line texts (digits ignored) in the top/bottom band of at least half the pages."""
    if len(pages) < 3:
        return set()
    seen: Counter[str] = Counter()
    for height, lines in pages:
        keys = {_header_key(ln.text) for ln in lines
                if ln.top < height * HEADER_BAND or ln.top > height * (1 - HEADER_BAND)}
        seen.update(k for k in keys if k)
    return {k for k, n in seen.items() if n >= len(pages) / 2}


def _join(a: str, b: str) -> str:
    """Join wrapped lines; keep a trailing hyphen and drop the space after it."""
    if not a:
        return b
    if a.endswith("-") and b[:1].isalpha():
        return a + b
    return f"{a} {b}"


HEADING_RE = re.compile(r"(?:[IVXL]{1,5}|[A-Z]|\d{1,2}|[a-z])\.\s+\S")


def _heading(ln: Line) -> bool:
    """Numbered or lettered section heading: "II. ANALYSIS", "B. Plaintiff Is Not ..."."""
    return bool(HEADING_RE.match(ln.text)) and len(ln.text) < 120


def _short(ln: Line, margin: float, right: float) -> bool:
    """Ends well before the right margin: the last line of a paragraph, or a heading."""
    return ln.x1 < right - 0.25 * (right - margin)


# ---------------------------------------------------------------- assembly

def assemble(pages: list[tuple[float, float, list[dict]]]) -> str | None:
    """pages: (width, height, words) per page, each word a dict with
    text, x0, x1, top and size, in reading order. Returns the rebuilt text."""
    all_words = [w for _, _, words in pages for w in words]
    if not all_words:
        return None
    body_size = _line_size(all_words)

    built = [(h, [_make_line(ws) for ws in _build_lines(words)]) for _, h, words in pages]
    headers = _running_headers(built)

    paragraphs: list[list] = []        # each: [text, [marker numbers]]
    footnotes: dict[int, str] = {}
    current_fn: int | None = None
    ended_in_block = False             # previous page's body ended inside a block quote
    ended_short = False                # ...or on a short line (a finished paragraph)

    # Pass 1: drop running headers and split each page into body and footnote area.
    split_pages: list[tuple[list[Line], list[Line]]] = []
    for (_, height, _), (_, lines) in zip(pages, built):
        lines = [ln for ln in lines if ln.text or ln.markers]
        lines = [ln for ln in lines
                 if not ((ln.top < height * HEADER_BAND or ln.top > height * (1 - HEADER_BAND))
                         and _header_key(ln.text) in headers)]
        # Footnote area: trailing run of lines smaller than body text.
        split = len(lines)
        while split > 0 and lines[split - 1].size < body_size * SMALL_RATIO:
            split -= 1
        split_pages.append((lines[:split], lines[split:]))

    # Margins come from the whole document: a short last page has no reliable margin.
    body_lines = [ln for body, _ in split_pages for ln in body]
    if not body_lines:
        return None
    # Left margin: the leftmost of the common line starts (block quotes are common too).
    starts = Counter(round(ln.x0) for ln in body_lines)
    top_count = max(starts.values())
    margin = min(x for x, n in starts.items() if n >= top_count / 2)
    right = sorted(ln.x1 for ln in body_lines)[int(0.9 * (len(body_lines) - 1))]

    # Pass 2: build paragraphs and footnotes.
    for body, notes in split_pages:
        starts: list[bool] = []                        # did body[i] start a paragraph?
        for i, ln in enumerate(body):
            prev = body[i - 1] if i else None
            indented = ln.x0 > margin + 15
            same_indent = prev is not None and abs(prev.x0 - ln.x0) < 3   # e.g. a block quote
            prev_short = prev is not None and _short(prev, margin, right)
            # Two lines at the same indent are a block; returning to the margin ends it.
            # (One indented line then the margin is just a paragraph's first line.)
            before = body[i - 2] if i > 1 else None
            # prev must continue the block: a heading plus a paragraph's indented
            # first line is not a block quote.
            leaving_block = (prev is not None and before is not None and not starts[i - 1]
                             and prev.x0 > margin + 15 and abs(before.x0 - prev.x0) < 3
                             and ln.x0 < prev.x0 - 15)
            new_para = (
                not paragraphs
                or (indented and not same_indent)              # indented first line or heading
                or prev_short                                  # short line ended a paragraph
                or leaving_block
                or _heading(ln)
                or (prev is not None and _heading(prev))
            )
            if prev is None and paragraphs and ln.x0 <= margin + 15 and not _heading(ln):
                new_para = ended_in_block or ended_short       # else: continues from previous page
            starts.append(new_para)
            text = ln.text
            nums = [n for _, n in ln.markers]
            for offset, n in sorted(ln.markers, reverse=True):
                text = text[:offset] + f"[^{n}]" + text[offset:]
            if new_para:
                paragraphs.append([text, nums])
            else:
                paragraphs[-1][0] = _join(paragraphs[-1][0], text)
                paragraphs[-1][1] += nums

        if body:
            ended_in_block = (len(body) > 1 and not starts[-1] and body[-1].x0 > margin + 15
                              and abs(body[-2].x0 - body[-1].x0) < 3)
            ended_short = _short(body[-1], margin, right)

        for ln in notes:
            text = ln.text
            # A footnote starts with its number set small, at the start of the line.
            if ln.markers and ln.markers[0][0] == 0:
                current_fn = ln.markers[0][1]
                footnotes[current_fn] = _join(footnotes.get(current_fn, ""), text)
            elif current_fn is not None:
                footnotes[current_fn] = _join(footnotes[current_fn], text)
            elif text:
                paragraphs.append([text, []])                  # small text with no footnote yet

    out: list[str] = []
    placed: set[int] = set()
    for text, nums in paragraphs:
        out.append(text)
        notes = [f"[^{n}]: {footnotes[n]}" for n in dict.fromkeys(nums)
                 if n in footnotes and n not in placed]
        placed.update(n for n in nums if n in footnotes)
        if notes:
            out.append("\n".join(notes))
    orphans = [f"[^{n}]: {t}" for n, t in sorted(footnotes.items()) if n not in placed]
    if orphans:
        out.append("Footnotes not matched to a reference in the text:\n" + "\n".join(orphans))
    return "\n\n".join(out)


def _letters(text: str) -> int:
    return sum(c.isalpha() for c in text)


def pdf_to_text(data: bytes, plain_text: str) -> str | None:
    """Footnote-aware text, or None if it would lose content compared with
    plain_text (the caller's ordinary extraction of the same PDF)."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(p.width, p.height,
                      p.extract_words(extra_attrs=["size"], use_text_flow=True))
                     for p in pdf.pages]
    except Exception:  # noqa: BLE001 - any parser failure means "use plain text"
        return None
    text = assemble(pages)
    if not text or _letters(text) < MIN_KEPT_RATIO * _letters(plain_text):
        return None
    return text
