"""Import paths for the tests and for the cocotb modules, which inherit sys.path from the runner."""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent

for path in (TESTS, REPO / "model", REPO / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def pytest_report_header(config):
    from importlib import metadata

    import hdl_sources
    import xfft_config

    versions = []
    for name in ("cocotb", "cocotb-vivado", "numpy", "pytest", "boto3", "moto"):
        try:
            versions.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            versions.append(f"{name} missing")
    return [f"python: {sys.prefix} ({', '.join(versions)})",
            f"xfft core: FFT_MODEL={hdl_sources.fft_model()} (rtl: generated core, engine: the C engine stand-in), "
            f"configuration {xfft_config.current().tag}"]
