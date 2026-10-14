"""Before/after DOM diff for the heal screen, over the stored (redacted) snapshots.

HTML-aware and word-level (audit L2): a tag becomes '<name', one token per attribute, and its
closer ('>' or '/>'); an end tag is one token; comments and the doctype are one token each;
text between tags splits on whitespace. So an attribute change is one removed and one added
token, and a label reword is one word -- never the whole line.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict

_TAG = re.compile(r"<!--.*?-->|<[^>]*>", re.S)
_ATTR = re.compile(r"""[^\s=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?""")

Kind = Literal["add", "remove", "same"]


class DiffSpan(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Kind
    text: str


def _tokens(html: str) -> list[tuple[str, bool]]:
    """(token, is_markup)."""
    tokens: list[tuple[str, bool]] = []
    pos = 0

    def text(chunk: str) -> None:
        tokens.extend((w, False) for w in chunk.split())

    for m in _TAG.finditer(html):
        text(html[pos : m.start()])
        tag = m.group(0)
        inner = tag[1:-1].strip()
        if inner.startswith(("!", "?")):
            tokens.append((tag, True))
        elif inner.startswith("/"):
            tokens.append((f"</{inner[1:].strip()}>", True))
        else:
            self_closing = inner.endswith("/")
            parts = _ATTR.findall(inner.rstrip("/").strip())
            if parts:
                tokens.append(("<" + parts[0], True))
                tokens.extend((part, True) for part in parts[1:])
                tokens.append(("/>" if self_closing else ">", True))
        pos = m.end()
    text(html[pos:])
    return tokens


def tokenize_html(html: str) -> list[str]:
    """A tag -> '<name', one token per attribute, '>' or '/>'; a closer -> '</name>';
    comments and doctype whole; text between tags split on whitespace."""
    return [token for token, _ in _tokens(html)]


def dom_diff(before: str, after: str) -> list[DiffSpan]:
    """Spans of one kind, each either all markup or all text -- so unchanged text reads as
    its own span next to a renamed tag, not glued to the tag's closer."""
    a, b = _tokens(before), _tokens(after)
    spans: list[DiffSpan] = []
    markup: list[bool] = []

    def emit(kind: Kind, words: list[tuple[str, bool]]) -> None:
        for word, is_markup in words:
            if spans and spans[-1].kind == kind and markup[-1] == is_markup:
                spans[-1] = DiffSpan(kind=kind, text=spans[-1].text + " " + word)
            else:
                spans.append(DiffSpan(kind=kind, text=word))
                markup.append(is_markup)

    matcher = SequenceMatcher(a=[t for t, _ in a], b=[t for t, _ in b], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            emit("same", a[i1:i2])
        else:
            emit("remove", a[i1:i2])
            emit("add", b[j1:j2])
    return spans
