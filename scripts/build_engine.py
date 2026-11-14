"""Builds the xfft stand-in's C engine (model/xfft_engine.c with model/xfft_dpi.c) for the current configuration
(XFFT_CONFIG) into <out dir>, for the SV wrapper (model/xfft_engine.sv, XSim and Verilator) and the VHDL wrapper
(model/xfft_engine.vhdl, NVC and GHDL). Needs a C compiler (gcc, or CC) and the C model of the generated core:
scripts/generate_ip.py runs Vivado once to generate it, after that the build needs no Vivado. Standard library
only.

    python scripts/build_engine.py <out dir>
    python scripts/build_engine.py --timing-only <out dir>

--timing-only (or XFFT_TIMING_ONLY=1): model/xfft_dpi_stub.c instead of model/xfft_dpi.c and AMD's C model, so
no Vivado and no generated core are needed. The timing is the same; every output sample is 0 and no frame
overflows (OVFLO 0). For timing tests where the data does not matter, such as CI.

Writes:
    <out dir>/xfft_model_pkg.sv     the configuration and its timing constants (model/xfft_timing.py) as an SV
                                    package; without measured constants it says so (XFFT_HAS_TIMING = 0), which
                                    is enough for the timing bench on the real core, not for the stand-in
    <out dir>/xfft_model_pkg.vhdl   the same constants as a VHDL package
    <out dir>/xfft_model.h          the same constants as a C header, compiled into the library
    <out dir>/xfft_engine.vhdl      the VHDL wrapper under the configuration's module name, when that is not
                                    xfft_core (else model/xfft_engine.vhdl is used as it is)
    <out dir>/cmodel/               AMD's bit-accurate C model: the generated core's cmodel output product,
                                    unzipped (not with --timing-only)
    <out dir>/xfft_cmodel_api.h     includes the C model's header and names its API prefix (both carry the IP
                                    version, read from the archive: model/cmodel_archive.py; not with
                                    --timing-only)
    <out dir>/libxfft_engine.so     the engine, exporting only xfft_engine_* (-fvisibility=hidden, -Bsymbolic)

The library finds the C model's libraries through its rpath $ORIGIN/cmodel, so <out dir> can be moved or copied
as a whole. It is rebuilt only when its sources, the constants, this script or the C-model archive change (stamp
file). The SV package names their digest, so a simulator build that hashes its sources sees a new library too.
Users: tests/sim_runner.py (DpiModel, FFT_MODEL=engine), tests/timing/xfft_timing_bench.py and the
characterization bench (characterize/bench/build_engine_char.py).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "model"), str(REPO / "scripts")]
import cmodel_archive  # noqa: E402
import generate_ip  # noqa: E402
import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402

LIB = "xfft_engine"
MODEL = REPO / "model"
C_SOURCES = [MODEL / "xfft_engine.c", MODEL / "xfft_dpi.c"]
TIMING_ONLY_SOURCES = [MODEL / "xfft_engine.c", MODEL / "xfft_dpi_stub.c"]
C_HEADERS = [MODEL / "xfft_engine.h"]
VHDL_WRAPPER = MODEL / "xfft_engine.vhdl"


def vhdl_wrapper(cfg: xfft_config.XfftConfig) -> str:
    """model/xfft_engine.vhdl with the entity named after the configuration's module."""
    text = VHDL_WRAPPER.read_text()
    renamed = text.replace("entity xfft_core is", f"entity {cfg.module} is").replace(
        "architecture c_engine of xfft_core is", f"architecture c_engine of {cfg.module} is")
    if renamed.count(cfg.module) < 2:
        sys.exit(f"build_engine: {VHDL_WRAPPER.name} has no entity xfft_core to rename")
    return renamed


def archive(cfg: xfft_config.XfftConfig) -> Path:
    """The C model of the configuration's core, generated with it; a clear message where that needs Vivado and
    Vivado is not there."""
    try:
        return cmodel_archive.find(generate_ip.generate(cfg))
    except cmodel_archive.CModelError as e:
        sys.exit(f"build_engine: {e}")
    except FileNotFoundError as e:
        sys.exit(f"build_engine: the C model of {cfg.tag} comes with the generated core, which needs Vivado once "
                 f"({e.filename} not found): run scripts/generate_ip.py where Vivado is installed, or copy its "
                 f"output {cfg.build_dir / 'ip_gen'} here")


def digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def timing_only_env() -> bool:
    return os.environ.get("XFFT_TIMING_ONLY", "").strip().lower() in ("1", "yes", "y", "on", "true")


def build(out: Path, timing_only: bool | None = None) -> None:
    """Build into out; timing_only (default: XFFT_TIMING_ONLY) links model/xfft_dpi_stub.c instead of the C model."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    timing_only = timing_only_env() if timing_only is None else timing_only
    cfg = xfft_config.current()
    try:
        timing = xfft_timing.constants(cfg)
    except LookupError:
        timing = None
    zip_path = None if timing_only else archive(cfg)
    sources = TIMING_ONLY_SOURCES if timing_only else C_SOURCES
    header = xfft_timing.c_header(cfg, timing)
    cc = shlex.split(os.environ.get("CC") or "gcc")
    # The constants are compiled in: the header is part of the digest, and so is the compiler
    lib_digest = hashlib.sha256((digest([Path(__file__).resolve(), *([zip_path] if zip_path else []), *sources,
                                         *C_HEADERS])
                                 + header + " ".join(cc) + (" timing-only" if timing_only else "")).encode()
                                ).hexdigest()
    write_if_changed(out / "xfft_model_pkg.sv", f"// libxfft_engine.so sources: sha256 {lib_digest}\n"
                     + xfft_timing.sv_package(cfg, timing))
    write_if_changed(out / "xfft_model_pkg.vhdl", xfft_timing.vhdl_package(cfg, timing))
    if cfg.module != "xfft_core":
        write_if_changed(out / "xfft_engine.vhdl", vhdl_wrapper(cfg))
    write_if_changed(out / "xfft_model.h", header)

    lib = out / f"lib{LIB}.so"
    stamp = out / "build_engine.sha256"
    if lib.exists() and stamp.exists() and stamp.read_text() == lib_digest:
        return
    if timing_only:
        compile_library(out, cc, [*cc, "-shared", "-fPIC", "-O2", "-std=gnu11", "-fvisibility=hidden",
                                       f"-I{out}", f"-I{MODEL}", *map(str, sources), "-o", str(lib),
                                       "-Wl,-Bsymbolic"])
        stamp.write_text(lib_digest)
        return
    try:
        names = cmodel_archive.names(zip_path)
    except cmodel_archive.CModelError as e:
        sys.exit(f"build_engine: {e}")
    cmodel = out / "cmodel"
    shutil.rmtree(cmodel, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(cmodel)
    (out / "xfft_cmodel_api.h").write_text(
        f"// Generated by scripts/build_engine.py from {zip_path.name}; do not edit.\n"
        f"#include \"{names.header}\"\n#define XFFT_API_PREFIX {names.api_prefix}\n")
    # -fvisibility=hidden: only the xfft_engine_* functions are exported (xfft_dpi.c is compiled in, hidden), so
    # the library shares the process with the simulator, cocotb and Python; -Bsymbolic: its own calls stay inside
    # it. --disable-new-dtags: DT_RPATH instead of DT_RUNPATH. DT_RPATH also applies to the dependencies of
    # dependencies, so the C model's own libraries in cmodel/ (gmp, mpfr, the floating-point C model, whatever
    # versions the archive has) are found without LD_LIBRARY_PATH, also in cocotb's Python process.
    # $ORIGIN/cmodel: cmodel/ beside the library wherever the directory is (a snapshot restored elsewhere, a copy
    # in a run directory).
    cmd = [*cc, "-shared", "-fPIC", "-O2", "-std=gnu11", "-fvisibility=hidden", f"-I{out}", f"-I{cmodel}",
           f"-I{MODEL}", *map(str, sources), "-o", str(lib), f"-L{cmodel}", f"-l{names.library}", "-lm",
           "-Wl,-Bsymbolic", "-Wl,--disable-new-dtags", "-Wl,-rpath,$ORIGIN/cmodel"]
    compile_library(out, cc, cmd)
    stamp.write_text(lib_digest)


def compile_library(out: Path, cc: list[str], cmd: list[str]) -> None:
    """Run the compiler command cmd, its output into out/cc.log."""
    with open(out / "cc.log", "w") as log:
        log.write(" ".join(cmd) + "\n")
        log.flush()
        try:
            result = subprocess.run(cmd, cwd=out, stdout=log, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            sys.exit(f"build_engine: C compiler {cc[0]} not found (set CC)")
    if result.returncode != 0:
        sys.exit(f"build_engine: {cc[0]} failed ({result.returncode}), see {out / 'cc.log'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    only = "--timing-only" in args
    args = [a for a in args if a != "--timing-only"]
    if len(args) != 1:
        sys.exit(__doc__)
    build(Path(args[0]), timing_only=True if only else None)
