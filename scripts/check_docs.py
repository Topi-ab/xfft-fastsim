"""Checks the Markdown files for misaligned ASCII diagrams and broken pipe tables.

    python scripts/check_docs.py [file.md ...]      default: README.md and docs/*.md

In fenced code blocks:
- only ASCII, no tabs (other characters render at other widths);
- boxes: a top edge +---+ and the next line below with + in the same two columns is a box; on every line in
  between, both edge columns must hold an edge character (| or +), so that text does not run into an edge;
- arrows: a standalone v must have a | or + above it, a standalone ^ one below it.
Outside code blocks, every row of a pipe table must have as many cells as its header.
Exit code 1 and one line per problem when something is wrong.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EDGE = "|+"


def code_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """(line number of the first line, lines) of each fenced code block."""
    blocks, start = [], None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            if start is None:
                start = i + 1
            else:
                blocks.append((start, lines[start:i]))
                start = None
    return blocks


def at(line: str, col: int) -> str:
    return line[col] if 0 <= col < len(line) else " "


def check_block(first: int, block: list[str]) -> list[str]:
    problems = []
    for k, line in enumerate(block):
        n = first + k + 1
        if "\t" in line:
            problems.append(f"line {n}: tab in a code block")
        if not line.isascii():
            problems.append(f"line {n}: non-ASCII character in a code block")
    # Boxes: top edges +----+ (corner, dashes, corner)
    for k, line in enumerate(block):
        for m in re.finditer(r"\+-+\+", line):
            left, right = m.start(), m.end() - 1
            bottom = next((j for j in range(k + 1, len(block))
                           if at(block[j], left) == "+" and at(block[j], right) == "+"
                           and set(block[j][left + 1:right]) <= set("-+")), None)
            if bottom is None:
                # A top edge whose box closes with corners in other columns
                below = [j for j in range(k + 1, len(block))
                         if at(block[j], left) in EDGE or at(block[j], right) in EDGE]
                if below:
                    problems.append(f"line {first + k + 1}: box at columns {left + 1}-{right + 1} has no bottom "
                                    f"edge with corners in the same columns")
                continue
            for j in range(k + 1, bottom):
                for col in (left, right):
                    if at(block[j], col) not in EDGE:
                        problems.append(f"line {first + j + 1}: column {col + 1} should be a box edge (| or +), "
                                        f"is {at(block[j], col)!r}")
    # Arrows
    for k, line in enumerate(block):
        for m in re.finditer(r"(?<![^ |])[v^](?![^ |])", line):
            col = m.start()
            other = k - 1 if m[0] == "v" else k + 1
            if not (0 <= other < len(block)) or at(block[other], col) not in EDGE:
                where = "above" if m[0] == "v" else "below"
                problems.append(f"line {first + k + 1}: arrow {m[0]!r} at column {col + 1} has no | or + {where} it")
    return problems


def cells(row: str) -> int:
    """Cells of a pipe-table row; | inside backticks or escaped does not count."""
    row = re.sub(r"`[^`]*`", "x", row.strip()).replace("\\|", "x")
    return len(row.strip("|").split("|"))


def check_tables(lines: list[str]) -> list[str]:
    problems, inside, header, fence = [], False, 0, False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
        if fence or not line.lstrip().startswith("|"):
            inside = False
            continue
        if not inside:
            inside, header = True, cells(line)
        elif cells(line) != header:
            problems.append(f"line {i + 1}: table row has {cells(line)} cells, the header {header}")
    return problems


def check(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    problems = check_tables(lines)
    for first, block in code_blocks(lines):
        problems += check_block(first, block)
    return [f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path}: {p}" for p in problems]


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] or [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
    problems = [p for f in files for p in check(f)]
    print("\n".join(problems) or f"check_docs: {len(files)} files, no problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
