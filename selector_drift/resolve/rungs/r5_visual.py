"""Rung 5 in v1: record the case, raise EscalateToHuman. Zero model calls (design 5.3).

The corpus case is metadata only -- field, description, the cascade's attempts, a hash of
the DOM. The page itself is never written here: every DOM snapshot is redacted before
storage and never leaves the client's boundary (Never #6), and the redacted copy already
lives in the run's evidence bundle (Task 33), findable by run_key and dom_sha256.

RungAttempt (resolve/trace.py), what the cascade hands over through Escalator, carries only
rung/family/outcome/ms -- Miss.detail (which rungs 1-4 may fill with page-derived text, e.g.
an accessible name or a header's text) is never part of it. `.as_json()` on each attempt is
therefore already metadata-only; nothing here re-attaches page text to an attempt.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from selector_drift.config import get_settings
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import Family, ResolvedElement, RungBase
from selector_drift.resolve.trace import Miss, RungAttempt
from selector_drift.spec.schema import FieldTarget

CORPUS_ENV = "SD_ESCALATION_CORPUS"  # read by Settings.escalation_corpus (Task 5)
DEFAULT_CORPUS = Path("/data/corpus/escalations")  # Settings' default: the worker's /data volume

# Defence in depth: FieldTarget.name is already pattern-constrained to
# ^[a-z][a-z0-9_]{0,63}$ by the spec schema, so a real FieldTarget can never carry a path
# separator or "..". record_case still never trusts that invariant blindly -- anything
# outside this charset is replaced before it ever reaches a filename. No `re` import here
# (the no-model-call AST allow-list is tight): plain str filtering does the job.
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c in _SAFE_CHARS else "_" for c in name).strip("._-") or "field"
    return cleaned[:64]


class VisualRung(RungBase):
    rung_id: int = 5
    family: Family = Family.NONE

    def __init__(self, corpus: Path | None = None) -> None:
        self._corpus = corpus

    @property
    def corpus(self) -> Path:
        # Through Settings, not os.environ (2026-09-26): one place reads configuration, and the
        # default is the writable volume Task 6 mounts on a read-only rootfs.
        return self._corpus or get_settings().escalation_corpus

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        self.escalate(page, target)
        raise AssertionError("unreachable: escalate() always raises")  # pragma: no cover

    def escalate(
        self,
        page: PageSource,
        target: FieldTarget,
        *,
        run_key: str | None = None,
        attempts: Sequence[RungAttempt] = (),
    ) -> ResolvedElement:
        case = self.record_case(page, target, run_key=run_key, attempts=attempts)
        raise EscalateToHuman(
            f"rungs 1-4 could not resolve {target.name!r}; "
            f"escalated to human review (case {case.name})",
            case=case,
        )

    def record_case(
        self,
        page: PageSource,
        target: FieldTarget,
        *,
        run_key: str | None,
        attempts: Sequence[RungAttempt],
    ) -> Path:
        dom_sha256 = hashlib.sha256(page.dom().encode("utf-8")).hexdigest()
        case = {
            "version": 1,
            "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
            "run_key": run_key,
            "field": target.name,
            "description": target.description,
            "relation": target.relation.value if target.relation else None,
            "attempts": [a.as_json() for a in attempts],
            "dom_sha256": dom_sha256,
            "evidence": "redacted, in the run's evidence bundle; never copied here",
            "model_calls": 0,
        }
        corpus = self.corpus
        corpus.mkdir(parents=True, exist_ok=True)
        path = corpus / f"{dom_sha256[:16]}-{_safe_name(target.name)}.json"
        # Never write outside the corpus directory, whatever a hostile/malformed target.name
        # resolves to once sanitised.
        if path.resolve().parent != corpus.resolve():
            raise ValueError(f"refusing to write a corpus case outside {corpus}: {path}")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(case, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)  # one case per (page, field): a rerun overwrites
        return path
