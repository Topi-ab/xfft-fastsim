"""Compares the real core and the stand-in (the C engine) on the variations of the characterization bench: the
rtl and the engine facts of each case, field by field.

    python characterize/bench/compare.py [cases...]     (python -m characterize runs it)

Default: the pinning cases and the known approximations of cases.py; name cases to compare those only (for
example the extra comparison cases after a run with --extra).

Timing fields: the input and output handshake clocks per frame, the config and status handshakes (clocks, and
the status words' values), the clocks of tready after reset; and the output checksum per frame (with the
bench's all-zero input data it still checks that both models give out the same words). Each is "identical" or
its first difference, and every difference fails, with two exceptions that are listed apart and do not fail:
- the output data of a frame that overflowed on both models (OVFLO in its status word on both): after an
  overflow the C model's data is not the core's (docs/USER_GUIDE.md, "Known limitations"); OVFLO itself must
  agree;
- the variations marked as known approximations in cases.py (docs/USER_GUIDE.md, "Known limitations"), and
  there only the fields known to differ (KNOWN_FIELDS: the input handshakes) by at most KNOWN_TOLERANCE clocks;
  every other field of them must be identical. One that has become identical is reported.
The event outputs (event_*) are not in the timing scope of the stand-in: their differences are listed apart, as
"events, not in timing scope", and do not fail. Exit code 0 when no other timing field differs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "model")]
import cases  # noqa: E402
import xfft_config  # noqa: E402

# The fields in which a known approximation may differ, and by how many clocks at most
KNOWN_FIELDS = ("input handshakes",)
KNOWN_TOLERANCE = 8


def first_difference(a: list, b: list, n: int | None = None) -> str | None:
    """None when equal, else where they first differ (frame and index when n is given)."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            where = f"frame {i // n} sample {i % n}" if n else f"#{i}"
            return f"{where}: rtl {x}, engine {y}"
    if len(a) != len(b):
        return f"length: rtl {len(a)}, engine {len(b)}"
    return None


def max_shift(a: list, b: list) -> int | None:
    """The largest clock difference between two lists of clocks of equal length (None: the lengths differ)."""
    return max((abs(x - y) for x, y in zip(a, b)), default=0) if len(a) == len(b) else None


def known_shift(r: dict, c: dict, field: str) -> int | None:
    """The clock difference of a field of KNOWN_FIELDS (None for another field)."""
    if field == "input handshakes":
        return max_shift(r["in_hs"], c["in_hs"])
    return None


def fields(r: dict, c: dict) -> dict[str, str | None]:
    """The timing fields (and the output checksums): field -> first difference or None."""
    n = r["n"]
    out = {
        "reset to tready": first_difference(
            [r["data_tready_first"] - r["reset_edge"], r["cfg_tready_first"] - r["reset_edge"]],
            [c["data_tready_first"] - c["reset_edge"], c["cfg_tready_first"] - c["reset_edge"]]),
        "input handshakes": first_difference(r["in_hs"], c["in_hs"], n),
        "output handshakes": first_difference(r["out_hs"], c["out_hs"], n),
        "config handshakes": first_difference(r["cfg_hs"], c["cfg_hs"]),
        "status clocks": first_difference([s[0] for s in r["status_hs"]], [s[0] for s in c["status_hs"]]),
        "status values": first_difference([s[1] for s in r["status_hs"]], [s[1] for s in c["status_hs"]]),
        "output data (checksums)": first_difference(*overflow_masked(r, c)),
    }
    return out


def overflowed(r: dict, c: dict) -> list[int]:
    """The frames whose status word has OVFLO set on both models."""
    return [k for k, (a, b) in enumerate(zip(r["status_hs"], c["status_hs"])) if a[1] & 1 and b[1] & 1]


def overflow_masked(r: dict, c: dict) -> tuple[list, list]:
    """The output checksums of both, those of the frames that overflowed on both left out (as None)."""
    skip = set(overflowed(r, c))
    return ([None if k in skip else x for k, x in enumerate(r["checksums"])],
            [None if k in skip else x for k, x in enumerate(c["checksums"])])


def event_fields(r: dict, c: dict) -> dict[str, str | None]:
    """The event outputs, not in the timing scope: event -> first difference or None."""
    return {e: first_difference(r["events"][e], c["events"][e]) for e in r["events"]}


def main() -> int:
    cfg = xfft_config.current()
    every = cases.cases(cfg.points, extra=True)
    unknown = [c for c in sys.argv[1:] if c not in every]
    if unknown:
        print(f"compare: unknown cases: {' '.join(unknown)}", file=sys.stderr)
        return 1
    names = {c: every[c] for c in sys.argv[1:]} if sys.argv[1:] else cases.cases(cfg.points)
    failures, events, missing, approx, identical = [], [], [], [], 0
    for name in names:
        paths = [cases.json_path(cfg.tag, name, m) for m in cases.MODELS]
        if not all(p.is_file() for p in paths):
            missing.append(name)
            continue
        r, c = (json.loads(p.read_text()) for p in paths)
        result = fields(r, c)
        diffs = {k: v for k, v in result.items() if v is not None}
        case = names[name]
        tag = " (extra)" if case.extra else " (known approximation)" if case.known else ""
        print(f"{name:26s}{tag} {'identical' if not diffs else f'{len(diffs)} timing field(s) differ'}"
              f"   (stand-in built with constants from {c.get('constants_source')})")
        for k, v in diffs.items():
            shift = known_shift(r, c, k) if case.known else None
            if shift is not None and shift <= KNOWN_TOLERANCE:
                approx.append(f"{name}: {k}: {v} (at most {shift} clocks apart)")
                print(f"    known {k}: {v} (at most {shift} clocks apart)")
            else:
                failures.append(f"{name}: {k}: {v}")
                print(f"    FAIL  {k}: {v}")
        if case.known and not diffs:
            print(f"    note  identical at this configuration; a known approximation at others ({case.known})")
        ov = overflowed(r, c)
        if ov and first_difference(r["checksums"], c["checksums"]):
            approx.append(f"{name}: output data of the frame(s) {ov} that overflowed on both models (C model)")
        identical += len(result) - len(diffs)
        events += [f"{name}: {k}: {v}" for k, v in event_fields(r, c).items() if v is not None]
    print(f"\ncompare: {len(names) - len(missing)} variations, {identical} timing fields identical, "
          f"{len(failures)} failures" + (f", missing: {' '.join(missing)}" if missing else ""))
    print(f"compare: known approximations (do not fail, docs/USER_GUIDE.md \"Known limitations\"): {len(approx)} "
          f"differences")
    for a in approx:
        print(f"  known: {a}")
    print(f"compare: events, not in timing scope (do not fail): {len(events)} differences")
    for e in events:
        print(f"  event: {e}")
    ok = not failures and not missing
    print(f"compare: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
