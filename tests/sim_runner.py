"""Build and run one elaboration on one simulator: XSim through cocotb-vivado (vivado), NVC or GHDL through
cocotb's own runners (VHDL only: the C engine's VHDL wrapper), or Verilator through cocotb's runner (SystemVerilog
only: the C engine's SV wrapper through DPI-C). Knows no DUT (the sources come from hdl_sources.py).

Build directories: build/sim/<build name>/<sim>[_<stand-in model>]/, so that a design with a stand-in builds
apart from the one with the real core, and every simulator apart from the others; switching between them
rebuilds none. A stand-in's C library is built into dpi/ in the build directory (dpi/<model name>/ each, when
several stand-ins share one simulation: all their libraries are loaded or linked). XSim snapshots go through the
elaboration cache (elab_cache.py); NVC and GHDL build in well under a second, Verilator in a few seconds (its
C++ compiles go through ccache when Verilator was built with it), and these always build afresh.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from cocotb_tools.runner import Verilog

import elab_cache
import riviera_ip
import sim_cache

REPO = Path(__file__).resolve().parents[1]
SIM_BUILD = REPO / "build" / "sim"
BUILD_LOG = "build.log"
SIM_LOG = "sim.log"


# Executable that shows a simulator is installed, and the hint when it is not
SIM_EXECUTABLE = {"vivado": "xelab", "nvc": "nvc", "ghdl": "ghdl", "verilator": "verilator", "riviera": "vsimsa"}
NOT_INSTALLED = {"vivado": "xelab not on PATH: source settings64.sh", "nvc": "nvc not on PATH",
                 "ghdl": "ghdl not on PATH", "verilator": "verilator not on PATH",
                 "riviera": "vsimsa of Riviera-PRO not on PATH"}
# What the version output of a simulator's executable must contain, where another tool has an executable of the
# same name (Active-HDL also has a vsimsa)
SIM_IDENTITY = {"riviera": ("-version", "Riviera-PRO")}
# VHDL-2008 on NVC and GHDL (the C engine's VHDL wrapper needs a 32-bit integer: not VHDL-2019 on NVC)
VHDL_STD = {"nvc": ["--std=2008"], "ghdl": ["--std=08"]}
# Verilator: the time unit of the sources without a `timescale (cocotb's clocks are in ns). Single-threaded: no
# --threads, the C engine's table of engines has no lock
VERILATOR_TIMESCALE = ("1ns", "1ps")
# Riviera: the same time unit (cocotb's Riviera runner takes no timescale: an alog option). Riviera simulates in
# one thread, as the C engine needs
RIVIERA_TIMESCALE = "1ns/1ps"
# Riviera: where the generated core is exported in the build directory (kept between builds, as dpi/)
RIVIERA_IP_DIR = "ip"


@cache
def installed(sim: str) -> bool:
    """Whether sim's executable is on PATH and, where SIM_IDENTITY names it, really is that simulator's."""
    exe = shutil.which(SIM_EXECUTABLE[sim])
    if exe is None or sim not in SIM_IDENTITY:
        return exe is not None
    option, name = SIM_IDENTITY[sim]
    try:
        out = subprocess.run([exe, option], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return name in out.stdout + out.stderr


def not_installed(sim: str) -> str:
    """Why sim counts as not installed."""
    if sim in SIM_IDENTITY and shutil.which(SIM_EXECUTABLE[sim]):
        return f"{SIM_EXECUTABLE[sim]} on PATH is not {SIM_IDENTITY[sim][1]}'s"
    return NOT_INSTALLED[sim]


def simulators(*sims: str) -> list:
    """pytest parameters for sims (default: vivado), each skipped where the simulator is not installed."""
    return [pytest.param(sim, marks=pytest.mark.skipif(not installed(sim), reason=not_installed(sim)))
            for sim in sims or ("vivado",)]


@dataclass(frozen=True)
class DpiModel:
    """A simulation model with a C library, standing in for a core: SystemVerilog through DPI-C (XSim, Verilator),
    and, when it has vhdl_sources, VHDL through VHPIDIRECT (NVC, GHDL).

    build: a script run as `python <build> <dir>` that writes lib<name>.so and the generated sources into <dir>
    (<build dir>/dpi/, inside the simulator's build directory, so the snapshot and its library move together).
    sources: the model's SystemVerilog in compile order, vhdl_sources its VHDL; a str is a file the build writes
    into <dir>, a Path a file of the repository. inputs: the files the model is made of, also generated ones such
    as the vendor's C-model archive (cache key, rebuild trigger). build_args: extra arguments of the build script
    before <dir> (for example --timing-only). env: (name, value) pairs set over the process's environment for the
    build (for example the model's own configuration), part of the cache key; a value that names a file enters the
    key by its contents.

    Several models can share one simulation (sim_runner.run() with all of them among its sources) when their
    names, their libraries' exported symbols and their HDL units differ: each is built into its own directory and
    every library is loaded or linked."""
    name: str
    build: Path
    sources: tuple
    inputs: tuple
    vhdl_sources: tuple = ()
    build_args: tuple = ()
    env: tuple = ()

    def cache_entry(self, file_entry) -> dict:
        """Fingerprint for sim_cache (file_entry: path -> {path, sha256})."""
        def entries(files):
            return [s if isinstance(s, str) else file_entry(s) for s in files]

        entry = {"kind": "DpiModel", "name": self.name, "build": file_entry(self.build),
                 "sources": entries(self.sources), "inputs": [file_entry(f) for f in self.inputs]}
        if self.vhdl_sources:
            entry["vhdl_sources"] = entries(self.vhdl_sources)
        if self.build_args:
            entry["build_args"] = list(self.build_args)
        if self.env:
            entry["env"] = {k: file_entry(Path(v)) if v and Path(v).is_file() else v for k, v in self.env}
        return entry

    def built(self, out: Path, files: tuple) -> list[Path]:
        """Build into out (with env over the process's environment); files as paths."""
        subprocess.run([sys.executable, str(self.build), *self.build_args, str(out)], check=True,
                       env=os.environ | dict(self.env) if self.env else None)
        return [out / s if isinstance(s, str) else Path(s) for s in files]

    def prepare(self, out: Path) -> tuple[list[Path], list[str]]:
        """Build into out; the HDL sources to compile and the xelab arguments that link the library."""
        hdl = self.built(out, self.sources)
        # -sv_root a subdirectory of the elaboration directory: xelab strips the elaboration directory from it, so
        # xsimk links ./dpi/lib<name>.so, relative to the test directory cocotb-vivado runs in (docs/CACHE.md)
        return hdl, ["-sv_root", str(out), "-sv_lib", f"lib{self.name}"]

    def prepare_vhdl(self, out: Path, sim: str) -> tuple[list[Path], dict[str, str], list[str]]:
        """Build into out for NVC or GHDL; the VHDL sources, the environment of the test process and the GHDL
        linker options. NVC finds VHPIDIRECT functions in the process: the library is preloaded. GHDL links them
        into its executable: the library and its rpath, and -rpath-link for the C model's own libraries."""
        if not self.vhdl_sources:
            raise ValueError(f"{self.name}: no VHDL sources, {sim} cannot simulate it")
        hdl = self.built(out, self.vhdl_sources)
        lib = out / f"lib{self.name}.so"
        if sim == "nvc":
            preload = ":".join(p for p in (str(lib), os.environ.get("LD_PRELOAD", "")) if p)
            return hdl, {"LD_PRELOAD": preload}, []
        return hdl, {}, [f"-Wl,{lib}", f"-Wl,-Wl,-rpath,{out}", f"-Wl,-Wl,-rpath-link,{out / 'cmodel'}"]

    def prepare_verilator(self, out: Path) -> tuple[list[Path], list[str]]:
        """Build into out for Verilator; the SV sources and the Verilator arguments that link the library into the
        executable: its path as a file argument, its rpath, and -rpath-link for the C model's own libraries."""
        if not self.sources:
            raise ValueError(f"{self.name}: no SystemVerilog sources, verilator cannot simulate it")
        hdl = self.built(out, self.sources)
        return hdl, [str(out / f"lib{self.name}.so"), "-LDFLAGS", f"-Wl,-rpath,{out} -Wl,-rpath-link,{out / 'cmodel'}"]

    def prepare_riviera(self, out: Path) -> tuple[list[Path], list[str]]:
        """Build into out for Riviera; the SV sources and the asim arguments that load the library (-sv_lib takes
        its path without the .so suffix; the library finds the C model's libraries through its own rpath)."""
        if not self.sources:
            raise ValueError(f"{self.name}: no SystemVerilog sources, riviera cannot simulate it")
        hdl = self.built(out, self.sources)
        return hdl, ["-sv_lib", str(out / f"lib{self.name}")]


def vivado_runner():
    """cocotb-vivado runner with two additions:

    - Verilog()-tagged build_args go to xvlog only (cocotb 2.1 hands them over with the tags stripped, and
      cocotb-vivado 0.1.1 would give them to xvhdl too, which rejects -d);
    - elab_args: extra xelab arguments (DPI libraries), which cocotb-vivado 0.1.1 has no option for.
    Neither is part of cocotb-vivado's own build signature; run() forces a build when they change."""
    from cocotb_vivado.runner import Vivado

    class TaggedVivado(Vivado):
        def _build_command(self):
            tagged = self._build_args
            self._build_args = [a for a in tagged if getattr(a, "tag", None) is None]
            try:
                cmds = list(super()._build_command())
            finally:
                self._build_args = tagged
            for cmd in cmds:
                if cmd[0] == "xvlog":
                    cmd += [str(a.value) for a in tagged if getattr(a, "tag", None) is Verilog]
            return cmds

        def _elab_command(self):
            return [*super()._elab_command(), *self.elab_args]

    runner = TaggedVivado()
    runner.elab_args = []
    return runner


def vhdl_runner(sim: str, link_args: Sequence[str] = ()):
    """cocotb's NVC or GHDL runner. GHDL: cocotb 2.1's build() runs `ghdl -m`, which already links the
    executable, without a way to pass linker options (elab_args belong to test()): link_args go into that
    command. NVC: cocotb 2.1's test() puts the process environment over extra_env, so an LD_PRELOAD the
    environment already has would replace the one that preloads the stand-in's library
    (DpiModel.prepare_vhdl(), which keeps the environment's entries in it): the runner puts extra_env's
    LD_PRELOAD back."""
    from cocotb_tools.runner import Ghdl, Nvc

    if sim == "nvc":
        class PreloadingNvc(Nvc):
            def _set_env_test(self):
                preload = self.env.get("LD_PRELOAD")
                super()._set_env_test()
                if preload is not None:
                    self.env["LD_PRELOAD"] = preload

        return PreloadingNvc()

    class LinkedGhdl(Ghdl):
        def _build_command(self):
            cmds = super()._build_command()
            make = cmds[-1]
            assert make[:2] == ["ghdl", "-m"], make
            make[-1:-1] = list(link_args)
            return cmds

    return LinkedGhdl()


def run(*, sources: Sequence, hdl_toplevel: str, test_module: str, build_name: str, sim: str = "vivado",
        hdl_toplevel_lang: str = "verilog", hdl_library: str = "xil_defaultlib",
        parameters: dict | None = None, verilog_defines: dict | None = None, testcase: str | None = None,
        extra_env: dict | None = None, cache: bool = False, cache_inputs: Sequence[Path] = (),
        cache_outputs: Sequence[str] = ()) -> None:
    """Build and test one elaboration on sim: vivado (XSim), nvc, ghdl or verilator.

    sources: HDL files in compile order, cocotb-vivado VivadoIp objects (generated IP, XSim only) and DpiModel
    objects (on NVC and GHDL their VHDL sources, on XSim and Verilator their SystemVerilog sources).
    hdl_toplevel_lang: "verilog" or "vhdl". cocotb-vivado encodes the port values by it (TOPLEVEL_LANG); left to
    cocotb's guess, a VHDL toplevel among Verilog sources would get Verilog-encoded values.
    hdl_library: library of the HDL sources. Generated IP compiles into xil_defaultlib (Vivado's simulation
    scripts put it there), so the design does too.
    verilog_defines: `define values for every xvlog call (XSim) or the verilator call. extra_env: environment of the
    cocotb test process.
    cache: look the run up in the result cache (sim_cache.py) and skip build and test on a hit; store it when it
    passes. Only for tests whose run-time inputs are all declared: cache_inputs are the files (or directories)
    the test or the HDL reads at run time, cache_outputs the files the test writes into the test directory
    (stored with the result and restored on a hit). A cached run uses a fixed seed (COCOTB_RANDOM_SEED, else
    sim_cache.DEFAULT_SEED), which is part of the key. Waves bypass the cache.
    On XSim the elaboration goes through the elaboration cache (elab_cache.py, SIM_ELAB_CACHE) whether the run is
    cached or not: a snapshot with the same build inputs is reused or restored instead of elaborated. NVC, GHDL and
    Verilator build afresh each time."""
    if sim not in SIM_EXECUTABLE:
        raise ValueError(f"sim={sim}: one of {', '.join(SIM_EXECUTABLE)}")
    parameters = dict(parameters or {})
    sources = distinct_models(list(sources))
    dpi_models = [s for s in sources if isinstance(s, DpiModel)]
    waves = env_true("WAVES")
    build_dir = SIM_BUILD / build_name / "_".join([sim, *(m.name for m in dpi_models)])
    build_args = [Verilog(a) for k, v in (verilog_defines or {}).items() for a in ("-d", f"{k}={v}")]
    extra_env = dict(extra_env or {})
    snapshot = hdl_toplevel.split(".")[-1]          # cocotb-vivado's snapshot name

    cached = None
    if cache:
        if waves:
            sim_cache.log("bypassed: waves requested")
        elif sim_cache.mode() != "off":
            cached = cache_run(
                sources=sources, cache_inputs=cache_inputs, cache_outputs=cache_outputs, test_module=test_module,
                extra_env=extra_env,
                run_args={"sim": sim, "hdl_toplevel": hdl_toplevel, "hdl_toplevel_lang": hdl_toplevel_lang,
                          "hdl_library": hdl_library, "test_module": test_module, "testcase": testcase,
                          "parameters": parameters, "verilog_defines": dict(verilog_defines or {})},
                test_id=f"{test_module}.{build_name}" + ("" if sim == "vivado" else f".{sim}")
                + (f".{testcase}" if testcase else ""), sim=sim)
            if cached is not None and sim_cache.mode() == "on":
                try:
                    # The build directory is emptied on a hit: only the stored files are there afterwards
                    if cached.lookup(build_dir, clear=build_dir.is_relative_to(SIM_BUILD)):
                        return
                except Exception as e:      # the cache never fails a test
                    sim_cache.log(f"lookup failed, running: {e}")

    test_args = dict(test_module=test_module, hdl_toplevel=hdl_toplevel, hdl_toplevel_library=hdl_library,
                     hdl_toplevel_lang=hdl_toplevel_lang, parameters=parameters, testcase=testcase,
                     build_dir=build_dir, test_dir=build_dir, waves=waves, extra_env=dict(extra_env or {}))
    start = time.monotonic()
    if sim == "verilator":
        runner = build_verilator(build_dir, sources, hdl_toplevel, parameters, verilog_defines or {}, waves)
        test_and_store(runner, cached, build_dir, test_args, cache_outputs, start)
        return
    if sim == "riviera":
        runner = build_riviera(build_dir, sources, hdl_toplevel, hdl_library, parameters, verilog_defines or {},
                               waves, test_args)
        test_and_store(runner, cached, build_dir, test_args, cache_outputs, start)
        return
    if sim != "vivado":
        runner = build_vhdl(sim, build_dir, sources, hdl_toplevel, hdl_library, parameters, waves, test_args)
        test_and_store(runner, cached, build_dir, test_args, cache_outputs, start)
        return

    runner = vivado_runner()
    # The elaboration cache: its key holds every build input, so a reused or restored snapshot is as good as a
    # fresh one, also for a result-cache miss. A waveform build has the absolute VCD path compiled in: never cached.
    elab = None
    if waves:
        elab_cache.log("bypassed: waves requested")
    else:
        elab = elab_cache.cached_elab(build_dir.relative_to(SIM_BUILD).as_posix(), lambda: elab_cache.elab_inputs(
            sources=sources, hdl_toplevel=hdl_toplevel, hdl_library=hdl_library, parameters=parameters,
            verilog_defines=verilog_defines))

    build_kwargs = dict(hdl_toplevel=hdl_toplevel, parameters=parameters, build_args=build_args,
                        build_dir=build_dir, hdl_library=hdl_library, waves=waves)
    if elab is not None:
        elaborate(runner, elab, build_dir, snapshot, sources, build_kwargs)
    elif cached is not None:
        # A miss builds from scratch: cocotb-vivado's build cache (build_signature.json, xvlog --incr) does not
        # see all of the key, and an old snapshot must not pass under the new key
        clear_build_dir(build_dir)
        logged(runner.build, build_dir / BUILD_LOG, sources=prepare_models(runner, build_dir, sources),
               always=True, clean=False, **build_kwargs)
    else:
        # What cocotb-vivado's build signature does not see (the models' files and build environments, the
        # defines, the xelab arguments) forces a build when it changes
        expanded = prepare_models(runner, build_dir, sources)
        h = hashlib.sha256(repr((runner.elab_args, verilog_defines)).encode())
        for model in dpi_models:
            h.update(repr(model.env).encode())
            for f in (model.build, *model.inputs):
                h.update(Path(f).read_bytes())
        stamp = build_dir / "extra_inputs.sha256"
        runner.build(sources=expanded, always=not stamp.exists() or stamp.read_text() != h.hexdigest(),
                     **build_kwargs)
        stamp.write_text(h.hexdigest())
    test_and_store(runner, cached, build_dir, test_args, cache_outputs, start)


def build_vhdl(sim: str, build_dir: Path, sources: list, hdl_toplevel: str, hdl_library: str, parameters: dict,
               waves: bool, test_args: dict):
    """Build on NVC or GHDL from scratch (dpi/ kept: the stand-in's build has its stamp there); the runner, ready
    for test(). The stand-in models become their VHDL sources; their environment goes into test_args. Under the
    same lock per build directory as elaborate()."""
    for src in sources:
        if type(src).__name__ == "VivadoIp":
            raise ValueError(f"{sim} cannot simulate the generated core (encrypted IP): XSim only")
    elab_cache.log(f"not used on {sim} (it builds in a second)")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(build_dir.parent / f".{build_dir.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        clear_build_dir(build_dir)
        models = [s for s in sources if isinstance(s, DpiModel)]
        expanded, link_args = [], []
        for src in sources:
            if isinstance(src, DpiModel):
                hdl, env, link = src.prepare_vhdl(dpi_dir(build_dir, src, len(models)), sim)
                expanded += hdl
                test_args["extra_env"] = merged_env(test_args["extra_env"], env)
                link_args += link
            else:
                expanded.append(src)
        runner = vhdl_runner(sim, link_args)
        logged(runner.build, build_dir / BUILD_LOG, sources=expanded, hdl_toplevel=hdl_toplevel,
               parameters=parameters, build_args=VHDL_STD[sim], build_dir=build_dir, hdl_library=hdl_library,
               waves=waves, always=True, clean=False)
    if sim == "ghdl":
        test_args["test_args"] = VHDL_STD[sim]      # ghdl -r takes the standard again
    return runner


def build_verilator(build_dir: Path, sources: list, hdl_toplevel: str, parameters: dict, defines: dict,
                    waves: bool):
    """Build on Verilator from scratch (dpi/ kept), with cocotb's runner (which adds --vpi and --public-flat-rw);
    the runner, ready for test(). The stand-in models become their SV sources, their libraries linked into the
    executable. Under the same lock per build directory as elaborate()."""
    from cocotb_tools.runner import get_runner

    for src in sources:
        if type(src).__name__ == "VivadoIp":
            raise ValueError("verilator cannot simulate the generated core (encrypted IP): XSim only")
    elab_cache.log("not used on verilator (it builds afresh; ccache speeds the C++ compiles if it was built with it)")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(build_dir.parent / f".{build_dir.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        clear_build_dir(build_dir)
        models = [s for s in sources if isinstance(s, DpiModel)]
        expanded, build_args = [], []
        for src in sources:
            if isinstance(src, DpiModel):
                hdl, args = src.prepare_verilator(dpi_dir(build_dir, src, len(models)))
                expanded += hdl
                build_args += args
            else:
                expanded.append(src)
        runner = get_runner("verilator")
        logged(runner.build, build_dir / BUILD_LOG, sources=expanded, hdl_toplevel=hdl_toplevel,
               parameters=parameters, defines=defines, build_args=build_args, timescale=VERILATOR_TIMESCALE,
               build_dir=build_dir, waves=waves, always=True, clean=False)
    return runner


def build_riviera(build_dir: Path, sources: list, hdl_toplevel: str, hdl_library: str, parameters: dict,
                  defines: dict, waves: bool, test_args: dict):
    """Build on Riviera from scratch (dpi/ and ip/ kept), with cocotb's runner (alog -sv, acom, the toplevel run
    by asim); the runner, ready for test(). The stand-in models become their SV sources, their libraries asim
    -sv_lib arguments in test_args. A generated core (VivadoIp) becomes its export for Riviera (riviera_ip.py,
    exported into ip/): its design-library files compiled here, its IP libraries mapped from the precompiled
    ones (XFFT_RIVIERA_LIBS), asim's -L list and glbl as a further toplevel. Under the same lock per build
    directory as elaborate()."""
    from cocotb_tools.runner import VHDL, get_runner

    elab_cache.log("not used on riviera (it builds afresh)")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(build_dir.parent / f".{build_dir.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        clear_build_dir(build_dir)
        models = [s for s in sources if isinstance(s, DpiModel)]
        ips = [s for s in sources if type(s).__name__ == "VivadoIp"]
        core = riviera_core(ips, build_dir / RIVIERA_IP_DIR, hdl_library) if ips else None
        expanded, sim_args, build_args = [], [], [Verilog("-timescale"), Verilog(RIVIERA_TIMESCALE)]
        for src in sources:
            if isinstance(src, DpiModel):
                hdl, args = src.prepare_riviera(dpi_dir(build_dir, src, len(models)))
                expanded += hdl
                sim_args += args
            elif type(src).__name__ == "VivadoIp":
                if src is ips[0]:
                    expanded += [VHDL(p) if lang == "vhdl" else Verilog(p) for p, lang, _ in core.sources]
            else:
                expanded.append(src)
        if core is not None:
            standards = {tuple(opts) for _, lang, opts in core.sources if lang == "vhdl"}
            if len(standards) > 1:
                raise ValueError(f"riviera: the core's VHDL files need different standards {sorted(standards)}")
            build_args += [VHDL(o) for o in (standards.pop() if standards else ())]
            if any(opts for _, lang, opts in core.sources if lang == "verilog"):
                raise ValueError("riviera: the core's Verilog files need compiler options, not supported")
            map_libraries(build_dir, core.libraries)
            sim_args += [a for lib in core.asim_libraries for a in ("-L", lib)] + core.tops
        runner = get_runner("riviera")
        logged(runner.build, build_dir / BUILD_LOG, sources=expanded, hdl_toplevel=hdl_toplevel,
               parameters=parameters, defines=defines, build_args=build_args, build_dir=build_dir,
               hdl_library=hdl_library, waves=waves, always=True, clean=False)
    test_args["test_args"] = [*test_args.get("test_args", []), *sim_args]
    return runner


def riviera_core(ips: list, out: Path, hdl_library: str) -> riviera_ip.RivieraIp:
    """The generated cores' export for Riviera (riviera_ip.py), exported into out; a clear error where it cannot
    be."""
    try:
        libs = riviera_ip.libs_dir()
        parts = []
        for ip in ips:
            parts += [riviera_ip.parse(d, hdl_library, libs) for d in riviera_ip.export(ip, out)]
        return riviera_ip.merge(parts)
    except riviera_ip.RivieraIpError as e:
        raise ValueError(f"riviera: the generated core: {e}") from e


def map_libraries(build_dir: Path, libraries: dict) -> None:
    """Map the precompiled libraries into the build directory's library.cfg (vsimsa amap), before the build."""
    script = build_dir / "map_libraries.do"
    script.write_text(riviera_ip.map_script(libraries))
    with open(build_dir / "map_libraries.log", "w") as log:
        result = subprocess.run(["vsimsa", "-do", script.name], cwd=build_dir, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"riviera: mapping the precompiled libraries failed, see {build_dir / 'map_libraries.log'}")


def test_and_store(runner, cached: sim_cache.CachedRun | None, build_dir: Path, test_args: dict,
                   cache_outputs: Sequence[str], start: float) -> None:
    """runner.test(); with a CachedRun, logged and the result stored when it passed."""
    if cached is None:
        runner.test(**test_args)
        return

    # Cached run: fixed seed; the build and test output into build.log and sim.log (echoed while they run); the
    # result stored when it passed. runner.test() leaves through sys.exit when a test fails, so a failure is
    # never stored. Declared outputs of an earlier run in a reused build directory are removed first.
    for name in cache_outputs:
        (build_dir / name).unlink(missing_ok=True)
    results = logged(runner.test, build_dir / SIM_LOG, seed=sim_cache.seed(), **test_args)
    tests, failed = result_counts(Path(results))
    if failed or not tests:
        sim_cache.log(f"not stored: {tests} tests, {failed} failed")
        return
    missing = [n for n in cache_outputs if not (build_dir / n).is_file()]
    if missing:
        sim_cache.log(f"not stored: declared outputs missing: {', '.join(missing)}")
        return
    try:
        cached.save(build_dir, [Path(results).relative_to(build_dir).as_posix(), BUILD_LOG, SIM_LOG,
                                *cache_outputs],
                    {"tests": tests, "seed": sim_cache.seed(), "wall_s": time.monotonic() - start},
                    overwrite=sim_cache.mode() == "refresh")
    except Exception as e:          # the cache never fails a test
        sim_cache.log(f"store failed: {e}")


def distinct_models(sources: list) -> list:
    """The sources with each stand-in model once (two instances of one configuration are one model); a clear error
    for two different models of the same name, which would share their directory and their library's symbols (names
    that differ only in case count as the same: VHDL names do not depend on case)."""
    out, seen = [], {}
    for src in sources:
        if isinstance(src, DpiModel):
            key = src.name.lower()
            if key in seen:
                if seen[key] != src:
                    raise ValueError(f"two different stand-in models named {src.name}: models that share a "
                                     f"simulation need different names (for the xfft core: different modules)")
                continue
            seen[key] = src
        out.append(src)
    return out


def merged_env(env: dict, more: dict) -> dict:
    """env with more over it, but LD_PRELOAD joined: the libraries of both, each once, more's first."""
    out = env | more
    if env.get("LD_PRELOAD") and more.get("LD_PRELOAD"):
        libs = [p for v in (more["LD_PRELOAD"], env["LD_PRELOAD"]) for p in v.replace(" ", ":").split(":") if p]
        out["LD_PRELOAD"] = ":".join(dict.fromkeys(libs))
    return out


def dpi_dir(build_dir: Path, model: DpiModel, count: int) -> Path:
    """Where a stand-in model is built: <build dir>/dpi (dpi/<name> with more than one model). Inside the
    elaboration directory, so the snapshot links ./dpi/lib<name>.so and runs from any copy of the directory."""
    root = build_dir / elab_cache.DPI_DIR
    return root if count == 1 else root / model.name


def prepare_models(runner, build_dir: Path, sources: list) -> list:
    """The sources with each stand-in model built and replaced by its HDL, its library on the xelab command."""
    models = [s for s in sources if isinstance(s, DpiModel)]
    out = []
    for src in sources:
        if isinstance(src, DpiModel):
            hdl, elab = src.prepare(dpi_dir(build_dir, src, len(models)))
            out += hdl
            runner.elab_args += elab
        else:
            out.append(src)
    return out


def clear_build_dir(build_dir: Path) -> None:
    """Empty a build directory under SIM_BUILD for a build from scratch, but for dpi/ and ip/: the stand-in's
    build script keeps its own stamp of its inputs in dpi/, the core's Riviera export (riviera_ip.py) in ip/."""
    if not build_dir.is_relative_to(SIM_BUILD):
        raise ValueError(f"{build_dir} is not a build directory under {SIM_BUILD}")
    if build_dir.is_dir():
        for p in build_dir.iterdir():
            if p.name in (elab_cache.DPI_DIR, RIVIERA_IP_DIR):
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()


def elaborate(runner, elab: elab_cache.CachedElab, build_dir: Path, snapshot: str, sources: list,
              build_kwargs: dict) -> None:
    """The snapshot through the elaboration cache, under a lock per build directory: the build directory's own
    when its stamp verifies, else the stored entry, else built from scratch and stored. Leaves runner ready for
    test(). Store problems are logged; the build then runs here."""
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(build_dir.parent / f".{build_dir.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reused = False
        if elab_cache.mode() == "on":
            try:
                reused = elab.local(build_dir, snapshot) or elab.restore(
                    build_dir, snapshot, replace=build_dir.is_relative_to(SIM_BUILD))
            except Exception as e:      # the cache never fails a test
                elab_cache.log(f"lookup failed, elaborating: {e}")
        if reused:
            runner.snapshot_name = snapshot     # cocotb-vivado sets it in build(), which is skipped
            return
        clear_build_dir(build_dir)
        start = time.monotonic()
        logged(runner.build, build_dir / BUILD_LOG, sources=prepare_models(runner, build_dir, sources),
               always=True, clean=False, **build_kwargs)
        try:
            elab.save(build_dir, snapshot, {"elab_s": time.monotonic() - start},
                      overwrite=elab_cache.mode() == "refresh")
        except Exception as e:          # the cache never fails a test
            elab_cache.log(f"store failed: {e}")


def env_true(name: str) -> bool:
    """An environment flag as cocotb reads it: 1, yes, y, on, true, enable (any case)."""
    return os.environ.get(name, "").strip().lower() in ("1", "yes", "y", "on", "true", "enable")


def cache_run(*, sources, cache_inputs, cache_outputs, test_module, extra_env, run_args,
              test_id, sim: str = "vivado") -> sim_cache.CachedRun | None:
    """The CachedRun of this run, or None (with the reason logged) when it cannot be cached."""
    try:
        inputs = {
            "run": run_args,
            "sources": [sim_cache.source_entry(s) for s in sources],
            "data": sim_cache.files_entries(cache_inputs),
            "python_modules": sim_cache.python_modules(test_module),
            "tools": sim_cache.tool_fingerprint(sim),
            "python": sim_cache.python_fingerprint(),
            "env": sim_cache.cocotb_env(extra_env),
            "seed": sim_cache.seed(),
            "outputs": list(cache_outputs),
        }
        return sim_cache.CachedRun(test_id, inputs)
    except Exception as e:          # the cache never fails a test
        sim_cache.log(f"disabled for {test_id}: {e}")
        return None


def logged(step, log_file: Path, **kwargs):
    """runner.build() / runner.test() with its output in log_file, copied to stdout while it runs.

    cocotb's runner sends the commands' stdout and stderr into log_file (stderr=STDOUT, in order)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.unlink(missing_ok=True)
    done = threading.Event()

    def echo() -> None:
        while not log_file.exists():
            if done.wait(0.2) and not log_file.exists():
                return
        with open(log_file, errors="replace") as f:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                elif done.is_set():
                    sys.stdout.write(f.read())
                    return
                else:
                    time.sleep(0.2)

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    try:
        return step(log_file=log_file, **kwargs)
    finally:
        done.set()
        thread.join()
        sys.stdout.flush()


def result_counts(results: Path) -> tuple[int, int]:
    """(tests, failed or errored) of a cocotb result XML."""
    tests = failed = 0
    for suite in ElementTree.parse(results).getroot().iter("testsuite"):
        tests += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    return tests, failed
