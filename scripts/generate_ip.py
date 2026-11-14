"""Generates the xfft core of the current configuration (model/xfft_config.py, XFFT_CONFIG) with Vivado: its
simulation files and its C model (the IP's cmodel output product), which the stand-in is built from.

    python scripts/generate_ip.py [out dir]        default: build/<configuration tag>/ip_gen/

Writes <out dir>/build_ip.tcl and runs it there: <out dir>/ip/<module>/<module>.xci, the simulation files and
<out dir>/ip/<module>/cmodel/*_bitacc_cmodel_lin64.zip. Runs Vivado only when the builder Tcl changed (stamp)
and holds a lock meanwhile, so that runs in parallel generate once. Needs vivado on PATH (settings64.sh).
The cocotb runs of the real core generate their own copy (cocotb-vivado's VivadoIp, the same builder Tcl).
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
import xfft_config  # noqa: E402

# The Vivado version this repository is tested with. Others are not refused: a different one only gets a warning.
TESTED_VIVADO = "2025.2"


def generate(cfg: xfft_config.XfftConfig, out: Path | None = None) -> Path:
    """Generate the core into out (default build/<tag>/ip_gen/) unless it is up to date; its IP directory."""
    out = Path(out or cfg.build_dir / "ip_gen").resolve()
    out.mkdir(parents=True, exist_ok=True)
    tcl = cfg.builder_tcl()
    digest = hashlib.sha256(tcl.encode()).hexdigest()
    ip_dir = out / "ip" / cfg.module
    stamp = out / "build_ip.sha256"
    with open(out / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if stamp.exists() and stamp.read_text() == digest and (out / cfg.xci).exists():
            return ip_dir
        stamp.unlink(missing_ok=True)
        (out / "build_ip.tcl").write_text(tcl)
        print(f"generate_ip: {cfg.tag} in {out}", flush=True)
        result = subprocess.run(["vivado", "-mode", "batch", "-nojournal", "-log", "vivado.log", "-source",
                                 "build_ip.tcl"], cwd=out, stdout=subprocess.DEVNULL)
        if result.returncode != 0:
            sys.exit(f"generate_ip: vivado failed ({result.returncode}), see {out / 'vivado.log'}")
        stamp.write_text(digest)
        warn_version(out / "vivado.log")
    return ip_dir


def warn_version(log: Path) -> None:
    """A warning when the Vivado that generated the core is not the tested one (nothing is refused)."""
    m = re.search(r"Vivado v(\d{4}\.\d+)", log.read_text(errors="replace")) if log.is_file() else None
    if m and m[1] != TESTED_VIVADO:
        print(f"generate_ip: warning: Vivado {m[1]}; this repository is tested with Vivado {TESTED_VIVADO}. "
              f"Measure the timing constants of your configuration (docs/CHARACTERIZATION.md).", flush=True)


def main() -> int:
    cfg = xfft_config.current()
    ip_dir = generate(cfg, Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"generate_ip: {ip_dir / (cfg.module + '.xci')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
