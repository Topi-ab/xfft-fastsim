"""The Markdown files have aligned ASCII diagrams and well-formed tables (scripts/check_docs.py): no Vivado."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import check_docs  # noqa: E402


@pytest.mark.parametrize("path", [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))],
                         ids=lambda p: p.name)
def test_docs_are_clean(path):
    assert check_docs.check(path) == []


def problems(tmp_path, text: str) -> list[str]:
    path = tmp_path / "x.md"
    path.write_text(text)
    return check_docs.check(path)


def test_checker_finds_misaligned_boxes_and_arrows(tmp_path):
    good = "```\n+----+\n| ab |\n+----+\n  |\n  v\n```\n"
    assert problems(tmp_path, good) == []
    assert any("column 6" in p for p in problems(tmp_path, "```\n+----+\n| abcd\n+----+\n```\n"))
    assert any("arrow" in p for p in problems(tmp_path, "```\n+----+\n+----+\n   v\n```\n"))
    assert any("no bottom" in p for p in problems(tmp_path, "```\n+----+\n|    |\n +----+\n```\n"))
    assert any("non-ASCII" in p for p in problems(tmp_path, "```\na \u2192 b\n```\n"))


def test_checker_counts_table_cells(tmp_path):
    assert problems(tmp_path, "| a | b |\n|---|---|\n| 1 | `x|y` |\n") == []
    assert any("cells" in p for p in problems(tmp_path, "| a | b |\n|---|---|\n| 1 | 2 | 3 |\n"))
