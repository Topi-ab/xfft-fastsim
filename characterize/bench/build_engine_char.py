"""Builds the stand-in (the C engine) like scripts/build_engine.py, but with the constants of the bench's
out/<tag>.json (build/characterize/out/) when extract.py has written one (they win over TABLE in
model/xfft_timing.py). Writes <out dir>/constants_source.txt, which names the source, so every engine run records
which constants it was built with.

    python characterize/bench/build_engine_char.py <out dir>
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "model"), str(REPO / "scripts")]
import build_engine  # noqa: E402
import cases  # noqa: E402
import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402


def main(out: Path) -> None:
    cfg = xfft_config.current()
    path = cases.OUT / f"{cfg.tag}.json"
    if path.is_file():
        constants = json.loads(path.read_text())["constants"]
        xfft_timing.constants = lambda c=None: dict(constants)     # what build_engine.build() asks for
        source = f"build/characterize/out/{path.name}"
    elif xfft_timing.table_tag(cfg) is not None:
        source = "TABLE in model/xfft_timing.py"
    elif xfft_timing.source_file(cfg) is not None:
        source = xfft_timing.source_file(cfg).relative_to(REPO).as_posix()
    else:
        source = "none (no constants for this configuration yet)"
    print(f"build_engine_char: {cfg.tag}: constants from {source}", flush=True)
    build_engine.build(out)
    (out.resolve() / "constants_source.txt").write_text(source + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
