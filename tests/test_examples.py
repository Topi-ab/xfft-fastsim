"""Smoke test: the examples run and pass on the stand-in, the C engine (FFT_MODEL=engine whatever FFT_MODEL says:
XSim, NVC, GHDL and Verilator, where installed). Skipped where no simulator for it is installed.

    pytest tests/test_examples.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL = "engine"
SIMULATORS = ("xelab", "nvc", "ghdl", "verilator")
needs_vivado = pytest.mark.skipif(shutil.which("xelab") is None, reason="xelab not on PATH: source settings64.sh")
needs_simulator = pytest.mark.skipif(not any(shutil.which(s) for s in SIMULATORS),
                                     reason=f"none of {', '.join(SIMULATORS)} on PATH")
# The examples' configuration (examples/conftest.py), unless XFFT_CONFIG names another
ENV = {**os.environ, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG") or "nfft=8", "FFT_MODEL": MODEL}


@needs_vivado
def test_plain_xsim():
    """examples/plain_xsim/run.sh on the stand-in (the SV wrapper of the C engine)."""
    r = subprocess.run(["bash", str(REPO / "examples" / "plain_xsim" / "run.sh")],
                       env={**ENV, "PYTHON": sys.executable}, capture_output=True, text=True)
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr


@needs_simulator
@pytest.mark.parametrize("example", ["cocotb_backpressure", "config_channel"])
def test_cocotb_example(example):
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                        str(REPO / "examples" / example)], cwd=REPO, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    sys.stdout.write(r.stdout[-2000:])
