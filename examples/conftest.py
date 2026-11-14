"""Import paths of the examples (the tests' infrastructure in tests/, model/, scripts/) and their configuration:
XFFT_CONFIG, by default nfft=8 (the 256-point core, whose timing constants ship in timing/)."""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = "nfft=8"

for path in (REPO / "tests", REPO / "model", REPO / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("XFFT_CONFIG", DEFAULT_CONFIG)


def pytest_report_header(config):
    import hdl_sources
    import xfft_config

    return [f"xfft core: FFT_MODEL={hdl_sources.fft_model()} (rtl: generated core, engine: the C engine stand-in), "
            f"configuration {xfft_config.current().tag}"]
