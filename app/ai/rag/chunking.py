"""Text extraction and chunking for knowledge documents (docs/ai/RAG_ARCHITECTURE.md §2).

Chunks are sized for the local embedding model, which reads only the first
128 word-piece tokens of a text (EMBEDDING_MAX_TOKENS). Token counts here are
an estimate (word pieces ≈ 1.35 x words for multilingual text); the target
stays well under the limit so nothing is cut off at embedding time.
"""

import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_SKIP_TAGS = {
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "noscript",
    "form",
    "svg",
}
_BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "tr",
    "br",
    "section",
    "article",
    "table",
    "ul",
    "ol",
    "pre",
    "blockquote",
}
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


def estimate_tokens(text: str) -> int:
    words = len(text.split())
    return math.ceil(words * 1.35) if words else 0


class _HtmlToText(HTMLParser):
    """HTML → plain text with markdown-style headings; drops navigation/scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Returns (title, text)."""
    parser = _HtmlToText()
    parser.feed(html)
    lines = [
        re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(parser.parts).splitlines()
    ]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return parser.title.strip(), text


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class Chunk:
    content: str
    heading_path: list[str] = field(default_factory=list)

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)


def _sections(text: str) -> list[tuple[list[str], str]]:
    """Split by markdown headings, tracking the heading path."""
    path: list[str] = []
    out: list[tuple[list[str], str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            out.append((list(path), body))
        buf.clear()

    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return out


def _pieces(body: str, max_tokens: int) -> list[str]:
    """Paragraphs, then sentences, then words, each at most max_tokens."""
    pieces: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", body)):
        if not para:
            continue
        if estimate_tokens(para) <= max_tokens:
            pieces.append(para)
            continue
        for sent in _SENTENCE_RE.split(para):
            if estimate_tokens(sent) <= max_tokens:
                pieces.append(sent)
                continue
            words = sent.split()
            step = max(1, int(max_tokens / 1.35))
            pieces.extend(
                " ".join(words[i : i + step]) for i in range(0, len(words), step)
            )
    return pieces


def chunk_text(text: str, *, max_tokens: int, overlap_tokens: int) -> list[Chunk]:
    """Heading-aware chunks of at most ~max_tokens, with word overlap between
    consecutive chunks of the same section. The heading path is prepended to
    each chunk so it is searchable and embedded with context."""
    chunks: list[Chunk] = []
    for path, body in _sections(normalize(text)):
        prefix = " › ".join(path)
        budget = max_tokens - estimate_tokens(prefix) - 2
        budget = max(budget, max_tokens // 2)
        current: list[str] = []
        # Pieces leave room for the overlap carried into the next chunk.
        for piece in _pieces(body, max(budget - overlap_tokens, budget // 2)):
            if current and estimate_tokens(" ".join(current + [piece])) > budget:
                chunks.append(Chunk(_with_prefix(prefix, " ".join(current)), path))
                n = int(overlap_tokens / 1.35)
                tail = " ".join(current).split()[-n:] if n else []
                current = [" ".join(tail)] if tail else []
                if estimate_tokens(" ".join(current + [piece])) > budget:
                    current = []
            current.append(piece)
        if current:
            chunks.append(Chunk(_with_prefix(prefix, " ".join(current)), path))
    return chunks


def _with_prefix(prefix: str, body: str) -> str:
    return f"{prefix}\n{body}" if prefix else body


def faq_chunks(pairs: list[tuple[str, str]]) -> list[Chunk]:
    """One chunk per question/answer pair (long answers are split)."""
    out: list[Chunk] = []
    for q, a in pairs:
        text = f"Q: {q.strip()}\nA: {a.strip()}"
        if estimate_tokens(text) <= 100:
            out.append(Chunk(text, ["FAQ"]))
        else:
            for c in chunk_text(a, max_tokens=90, overlap_tokens=10):
                out.append(Chunk(f"Q: {q.strip()}\nA: {c.content}", ["FAQ"]))
    return out
