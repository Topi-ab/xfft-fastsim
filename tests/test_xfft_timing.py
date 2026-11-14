"""The timing constants checked by the plain-XSim timing bench (tests/timing/xfft_timing_bench.py validate), on
the model FFT_MODEL selects: on rtl a check of the constants against the real core, on engine a check that the
stand-in follows them. All cases run in parallel, at most XFFT_TIMING_JOBS at a time (default: all).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hdl_sources

BENCH = Path(__file__).resolve().parent / "timing" / "xfft_timing_bench.py"


@pytest.mark.parametrize("sim", hdl_sources.simulators(xsim_only="a plain XSim bench"))
def test_xfft_timing(sim: str) -> None:
    cmd = [sys.executable, str(BENCH), "validate", "--model", hdl_sources.fft_model(),
           "-j", os.environ.get("XFFT_TIMING_JOBS", "0")]
    result = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(result.stdout + result.stderr)
    assert result.returncode == 0, "timing validation failed: " + " | ".join(
        line for line in result.stdout.splitlines() if line.startswith("FAIL"))
