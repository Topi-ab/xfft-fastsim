"""The C engine against the real core's input acceptance after input gaps, with the output blocked.

tests/engine_c/gap_vectors.json.gz has every vector measured on the real core for three transform lengths:
gaps ([d, g]: g clocks with s_axis_data_tvalid low before input sample F - d) and the shortfall, F minus the samples
accepted before the first refused clock (F: the samples accepted with no gaps). The engine is compiled with a C
compiler (CC, else cc or gcc) for each configuration, with stand-ins for the C model's functions
(model/xfft_dpi_stub.c: the timing does not depend on the data), and tests/engine_c/gap_driver.c runs every
vector. No simulator and no Vivado needed; skipped without a C compiler.
"""

from __future__ import annotations

import gzip
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))
import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402

HERE = ROOT / "tests" / "engine_c"
VECTORS = json.loads(gzip.decompress((HERE / "gap_vectors.json.gz").read_bytes()))["configs"]


def compiler() -> list[str] | None:
    if os.environ.get("CC"):
        return shlex.split(os.environ["CC"])
    for name in ("cc", "gcc"):
        if shutil.which(name):
            return [name]
    return None


@pytest.mark.parametrize("points", sorted(VECTORS, key=int))
def test_engine_gaps(points: str, tmp_path: Path) -> None:
    cc = compiler()
    if cc is None:
        pytest.skip("no C compiler (set CC)")
    entry = VECTORS[points]
    cfg = xfft_config.XfftConfig(**entry["config"])
    (tmp_path / "xfft_model.h").write_text(xfft_timing.c_header(cfg, entry["timing"]))
    exe = tmp_path / "gap_driver"
    subprocess.run([*cc, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{tmp_path}", f"-I{ROOT / 'model'}",
                    "-o", str(exe), str(ROOT / "model" / "xfft_engine.c"), str(ROOT / "model" / "xfft_dpi_stub.c"),
                    str(HERE / "gap_driver.c")], check=True)
    vectors = entry["vectors"]
    stdin = "".join(" ".join(f"{d}:{g}" for d, g in gaps) + "\n" for gaps, _ in vectors)
    lines = subprocess.run([str(exe)], input=stdin, capture_output=True, text=True, check=True).stdout.splitlines()
    assert lines[0] == f"F {entry['timing']['INPUT_LIMIT']}", lines[0]
    got = [int(x) for x in lines[1:]]
    assert len(got) == len(vectors)
    misses = [(gaps, want, have) for (gaps, want), have in zip(vectors, got) if want != have]
    assert not misses, f"{len(misses)} of {len(vectors)} differ, first: gaps {misses[0][0]} real core " \
                       f"{misses[0][1]}, engine {misses[0][2]}"
