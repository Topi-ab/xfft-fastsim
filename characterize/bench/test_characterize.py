"""Characterization bench: every variation of cases.py on the real core (rtl) and on the stand-in (engine: the C
engine through its SV wrapper, on XSim as the real core), one cached run each (sim_runner.run(cache=True)). A run
records facts only, into <case>.json in its test directory: every config, input, output and status handshake
clock, every clock an event output is high, the clocks of tready after reset, and a checksum of each frame's
output words. It asserts nothing about timing constants; extract.py derives them, compare.py compares the two
models. The input data are zeros (the handshakes do not depend on them, and zeros simulate faster), full-scale
random data in a case that sets full_scale (cases.py).

Each model is compiled and elaborated once per build key (snapshots.py: a lock, so that parallel cases build it
once, and a stamp with the hash of every snapshot file, verified before each use), or restored from the
elaboration cache (elab_cache.py) when another run or machine has stored it; every case runs in its own directory
with a verified copy of the snapshot and its dpi/, so parallel cases share nothing. The result cache stays per case:
its key is the variation plus the model's inputs.
On a hit nothing is built. The rtl runs depend on the core only. The engine runs build the stand-in from the
constants in out/<tag>.json once extract.py has written it (else from TABLE), and that file is one of their
inputs.

    python -m characterize          (runs these, then the extraction and the comparison)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from pathlib import Path

import cocotb
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge
from cocotb_tools.runner import Verilog

import cases
import elab_cache
import hdl_sources
import sim_cache
import sim_runner
import snapshots
import xfft_config
import xfft_tb
from xfft_tb import EVENTS, Frame, Lane

HERE = Path(__file__).resolve().parent
LIBRARY = "xil_defaultlib"
PERIOD_NS = 10


def frames_of(cfg: xfft_config.XfftConfig, case: cases.Case) -> list[Frame]:
    """The case's frames: all-zero data (case.full_scale: fixed full-scale random data, see cases.py), forward
    and inverse alternating, both scaling schedules."""
    import numpy as np

    rng = np.random.default_rng(11)
    data = ((lambda: xfft_tb.random_frame(cfg, rng, full_scale=True)) if case.full_scale
            else (lambda: np.zeros(cfg.points, complex)))
    return [Frame(data(), f % 2 == 0,
                  xfft_tb.schedule_a(cfg) if f % 3 != 2 else xfft_tb.schedule_b(cfg),
                  dict(case.gaps.get(f, {})), dict(case.stalls.get(f, {}))) for f in range(case.frames)]


def core_version() -> str:
    """The xfft version of the core this run simulated: from the generated .xci (rtl) or from the C-model
    archive the stand-in was built from (engine)."""
    for xci in Path("../snapshot_rtl").glob("ip/*/*.xci"):
        m = re.search(r"xilinx\.com:ip:xfft:(\d+\.\d+)", xci.read_text(errors="replace"))
        if m:
            return m[1]
    api = Path(elab_cache.DPI_DIR) / "xfft_cmodel_api.h"          # the run directory's copy of dpi/
    if api.is_file():
        m = re.search(r"xfft_v(\d+)_(\d+)_bitacc_cmodel", api.read_text())
        if m:
            return f"{m[1]}.{m[2]}"
    return "unknown"


class Monitor:
    """Samples the core's handshakes and events at every clock edge, values from before the edge."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.done = False
        self.facts = {"reset_edge": None, "data_tready_first": None, "cfg_tready_first": None,
                      "cfg_valid": [], "cfg_hs": [], "in_hs": [], "in_refused": [], "out_hs": [],
                      "out_stalled": [], "status_hs": [], "events": {e: [] for e in EVENTS}}

    async def run(self) -> None:
        d, f, cycle = self.dut, self.facts, 0
        v = xfft_tb.value
        while not self.done:
            await RisingEdge(d.aclk)
            if v(d.aresetn):
                if f["reset_edge"] is None:
                    f["reset_edge"] = cycle
                if f["data_tready_first"] is None and v(d.s_axis_data_tready):
                    f["data_tready_first"] = cycle
                if f["cfg_tready_first"] is None and v(d.s_axis_config_tready):
                    f["cfg_tready_first"] = cycle
            if v(d.s_axis_config_tvalid):
                f["cfg_valid"].append(cycle)
                if v(d.s_axis_config_tready):
                    f["cfg_hs"].append(cycle)
            if v(d.s_axis_data_tvalid):
                (f["in_hs"] if v(d.s_axis_data_tready) else f["in_refused"]).append(cycle)
            if v(d.m_axis_data_tvalid):
                (f["out_hs"] if v(d.m_axis_data_tready) else f["out_stalled"]).append(cycle)
            if v(d.m_axis_status_tvalid) and v(d.m_axis_status_tready):
                f["status_hs"].append([cycle, v(d.m_axis_status_tdata)])
            for e in EVENTS:
                if v(getattr(d, e)):
                    f["events"][e].append(cycle)
            cycle += 1


@cocotb.test()
async def characterize(dut):
    """The variation named by CHAR_CASE: drive it, record everything, write <case>.json."""
    cfg = xfft_config.current()
    case = cases.cases(cfg.points, extra=True)[os.environ["CHAR_CASE"]]
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    lane = Lane(dut, cfg)
    lane.idle()
    dut.aresetn.value = 0
    monitor = Monitor(dut)
    cocotb.start_soon(monitor.run())
    await ClockCycles(dut.aclk, 30)
    dut.aresetn.value = 1
    frames = frames_of(cfg, case)
    result = await lane.run(dut.aclk, frames, timeout=(case.frames + 12) * 6 * cfg.points)
    await ClockCycles(dut.aclk, 2)
    monitor.done = True
    checksums = [hashlib.sha256(json.dumps([o.data, o.user, o.last]).encode()).hexdigest()[:16]
                 for o in result.frames]
    facts = {"case": case.name, "model": os.environ["FFT_MODEL"], "config": cfg.tag, "n": cfg.points,
             "core_version": core_version(), "frames": case.frames,
             "gaps": {str(f): {str(k): v for k, v in g.items()} for f, g in case.gaps.items()},
             "stalls": {str(f): {str(k): v for k, v in s.items()} for f, s in case.stalls.items()},
             "constants_source": ((Path(elab_cache.DPI_DIR) / "constants_source.txt").read_text().strip()
                                  if os.environ["FFT_MODEL"] == "engine" else "the real core"),
             "checksums": checksums, **monitor.facts}
    Path(f"{case.name}.json").write_text(json.dumps(facts))
    dut._log.info("%s on %s: %d input, %d output handshakes, %d status words; wrote %s.json", case.name,
                  facts["model"], len(facts["in_hs"]), len(facts["out_hs"]), len(facts["status_hs"]), case.name)


def stand_in(cfg: xfft_config.XfftConfig) -> sim_runner.DpiModel:
    """The repo's stand-in (the C engine), built by build_engine_char.py: with the constants of out/<tag>.json when
    it exists. build_engine_char.py runs scripts/build_engine.py's build(), so that script stays an input (keys,
    rebuilds)."""
    base = hdl_sources.xfft_engine(cfg)
    constants = cases.OUT / f"{cfg.tag}.json"
    inputs = (*base.inputs, base.build, HERE / "build_engine_char.py",
              *([constants] if constants.is_file() else []))
    return dataclasses.replace(base, build=HERE / "build_engine_char.py", inputs=inputs)


def snapshot(cfg: xfft_config.XfftConfig, model: str, sources: list, defines: dict) -> tuple[Path, dict]:
    """The model's verified snapshot for this configuration and its stamp (snapshots.py): built once per build key,
    or restored from the elaboration cache (elab_cache.py), which the flow tests' builds share."""
    root = cases.BUILD / cfg.tag / f"snapshot_{model}"

    def build(target: Path) -> Path | None:
        # The stand-in into target/dpi (sim_runner.dpi_dir()): xelab strips the elaboration directory from
        # -sv_root as a string prefix, so the snapshot links ./dpi/libxfft_engine.so. ensure() emptied target, and
        # clean=False keeps dpi/.
        runner = sim_runner.vivado_runner()
        target.mkdir(parents=True, exist_ok=True)
        expanded = sim_runner.prepare_models(runner, target, sources)
        sim_runner.logged(runner.build, target / sim_runner.BUILD_LOG, sources=expanded, hdl_toplevel=cfg.module,
                          build_args=[Verilog(a) for k, v in defines.items() for a in ("-d", f"{k}={v}")],
                          build_dir=target, hdl_library=LIBRARY, always=True, clean=False)
        return None

    inputs = snapshots.build_inputs(sources, defines, cfg.module, LIBRARY)
    elab = elab_cache.cached_elab(f"characterize/{cfg.tag}/snapshot_{model}", lambda: inputs)
    return root, snapshots.ensure(root, cfg.module, inputs, build, elab=elab)


def params() -> list:
    names = cases.cases(xfft_config.current().points, extra=True)
    return [pytest.param(c, m, id=f"{c}-{m}") for c in names for m in cases.MODELS]


@pytest.mark.parametrize("case,model", params())
def test_characterize(case: str, model: str) -> None:
    cfg = xfft_config.current()
    sources = [stand_in(cfg) if model == "engine" else hdl_sources.xfft_ip(cfg)]
    defines = {"XFFT_CORE_NAME": cfg.module}
    lang = hdl_sources.toplevel_lang(model)
    extra_env = {"CHAR_CASE": case, "FFT_MODEL": model, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")}
    output = f"{case}.json"
    run_dir = cases.run_dir(cfg.tag, case, model)
    # The cache key as sim_runner.run() makes it: the sources (core or stand-in inputs), the run arguments, the
    # Python import closure, the tools, the environment of the test process (the variation) and the seed
    cached = None
    if sim_cache.mode() != "off":
        cached = sim_runner.cache_run(
            sources=sources, cache_inputs=[], cache_outputs=[output], test_module=__name__, extra_env=extra_env,
            run_args={"sim": "vivado", "hdl_toplevel": cfg.module, "hdl_toplevel_lang": lang,
                      "hdl_library": LIBRARY, "test_module": __name__, "testcase": None, "parameters": {},
                      "verilog_defines": defines},
            test_id=f"{__name__}.{cfg.tag}/{case}_{model}")
        if cached is not None and sim_cache.mode() == "on" and cached.lookup(run_dir, clear=True):
            snapshots.mark_run_dir(run_dir)       # the restored outputs live in a run directory too
            return
    snap, stamp = snapshot(cfg, model, sources, defines)
    snapshots.prepare_run_dir(snap, cfg.module, stamp, run_dir)
    runner = sim_runner.vivado_runner()
    runner.snapshot_name = cfg.module           # cocotb-vivado sets it in build(), which this process skips
    start = time.monotonic()
    results = sim_runner.logged(runner.test, run_dir / sim_runner.SIM_LOG, test_module=__name__,
                                hdl_toplevel=cfg.module, hdl_toplevel_library=LIBRARY, hdl_toplevel_lang=lang,
                                build_dir=run_dir, test_dir=run_dir, seed=sim_cache.seed(), extra_env=extra_env)
    tests, failed = sim_runner.result_counts(Path(results))
    assert tests and not failed, f"{case} on {model}: {tests} tests, {failed} failed"
    if cached is not None:
        try:
            cached.save(run_dir, [Path(results).relative_to(run_dir).as_posix(), sim_runner.SIM_LOG, output],
                        {"tests": tests, "seed": sim_cache.seed(), "wall_s": time.monotonic() - start,
                         "snapshot": stamp["snapshot"]["digest"]},
                        overwrite=sim_cache.mode() == "refresh")
        except Exception as e:          # the cache never fails a run
            sim_cache.log(f"store failed: {e}")
