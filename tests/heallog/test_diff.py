from __future__ import annotations

import pytest

from selector_drift.heallog.diff import dom_diff, tokenize_html


@pytest.mark.unit
def test_diff_is_word_level_not_line_level() -> None:
    # Fixed 2026-09-26 (audit L2): the tokenizer is HTML-aware -- a tag becomes its
    # name, one token per attribute, and its closer -- so an attribute change is one
    # removed and one added token rather than the whole tag.
    spans = dom_diff("<td class=a>X</td>", "<td class=b>X</td>")
    assert [s.text for s in spans if s.kind == "remove"] == ["class=a"]
    assert [s.text for s in spans if s.kind == "add"] == ["class=b"]


@pytest.mark.unit
def test_the_tokenizer_splits_a_tag_into_name_attributes_and_closer() -> None:
    assert tokenize_html('<td class="a b" id=x>Claim Number</td>') == [
        "<td",
        'class="a b"',
        "id=x",
        ">",
        "Claim",
        "Number",
        "</td>",
    ]


@pytest.mark.unit
def test_a_label_reword_changes_one_word() -> None:
    spans = dom_diff("<th>Claim Number</th>", "<th>Claim Reference</th>")
    assert [s.text for s in spans if s.kind == "remove"] == ["Number"]
    assert [s.text for s in spans if s.kind == "add"] == ["Reference"]


@pytest.mark.unit
def test_a_tag_rename_shows_the_names_not_the_whole_row() -> None:
    spans = dom_diff("<table><tr><td>X</td></tr></table>", "<div><div><div>X</div></div></div>")
    assert "X" in [s.text for s in spans if s.kind == "same"]


@pytest.mark.unit
def test_comments_and_doctype_are_single_tokens() -> None:
    assert tokenize_html("<!doctype html><!-- a b -->") == ["<!doctype html>", "<!-- a b -->"]
