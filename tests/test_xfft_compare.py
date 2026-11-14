"""The real core against the stand-in (the C engine), word by word (always both models; FFT_MODEL does not apply).

Toplevel xfft_pair.sv: the generated core and the stand-in (the C engine's SV wrapper) side by side. Each gets the
same frames through its own ports and handshakes, so a difference in timing does not change what either sees.
Frame plan (8 frames,
sent back to back as far as each core takes them):
    0 to 3  random, forward and inverse alternating, two scaling schedules, no gaps
    4       forward, SCALE_SCH 0, full-scale random data: overflows
    5       1-clock input gaps (about 1 in 64 samples) and 1-clock output stalls (about 1 in 16)
    6       as 5, plus a 50-clock input gap at sample N/2 and a 50-clock output stall at output N/3
    7       random gaps of 1 to 4 clocks (1 in 50) and stalls of 1 to 8 clocks (1 in 20)
Checks: every output word (data, XK_INDEX, OVFLO, the other tuser bits, tlast) identical, except the data of
the overflowing frame 4 (the C model's data after an overflow differs from the core's; OVFLO must agree), the
status words and their clocks identical, and the per-frame latencies within LATENCY_TOLERANCE clocks. The differences
and the latencies of both go to xfft_compare_results.json in the test directory.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import cocotb
import numpy as np
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

import hdl_sources
import sim_runner
import xfft_config
import xfft_tb
from xfft_tb import Frame, Lane

PERIOD_NS = 4
RESULTS = Path("xfft_compare_results.json")
OVERFLOW_FRAME = 4
# Largest difference between the core's and the stand-in's latency of a frame, in clocks: the stand-in must match
# exactly (and so must every input and output handshake clock and every status word's clock)
LATENCY_TOLERANCE = 0


def plan(cfg: xfft_config.XfftConfig) -> list[Frame]:
    rng = np.random.default_rng(7)
    rnd = random.Random(7)
    n = cfg.points
    frames = []
    for f in range(8):
        gaps, stalls = {}, {}
        if f in (5, 6):
            gaps = {k: 1 for k in range(1, n) if rnd.random() < 1 / 64}
            stalls = {k: 1 for k in range(n) if rnd.random() < 1 / 16}
        if f == 6:
            gaps[n // 2] = 50
            stalls[n // 3] = 50
        if f == 7:
            gaps = {k: rnd.randint(1, 4) for k in range(1, n) if rnd.random() < 0.02}
            stalls = {k: rnd.randint(1, 8) for k in range(n) if rnd.random() < 0.05}
        if f == OVERFLOW_FRAME:
            frames.append(Frame(xfft_tb.random_frame(cfg, rng, full_scale=True), True, [0] * cfg.stages))
        else:
            frames.append(Frame(xfft_tb.random_frame(cfg, rng), f % 2 == 0,
                                xfft_tb.schedule_a(cfg) if f % 4 < 2 else xfft_tb.schedule_b(cfg), gaps, stalls))
    return frames


@cocotb.test()
async def compare(dut):
    cfg = xfft_config.current()
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    core, model = Lane(dut, cfg, "core_"), Lane(dut, cfg, "model_")
    core.idle()
    model.idle()
    dut.aresetn.value = 0
    await ClockCycles(dut.aclk, 50)
    dut.aresetn.value = 1
    frames = plan(cfg)
    tasks = [cocotb.start_soon(lane.run(dut.aclk, frames)) for lane in (core, model)]
    rc, rm = [await t for t in tasks]

    ov = cfg.ovflo_bit
    result = {"frames": [], "status_core": rc.status, "status_model": rm.status,
              "events_core": rc.events, "events_model": rm.events}
    failures = []
    for f, (oc, om) in enumerate(zip(rc.frames, rm.frames)):
        d = {"data": sum(a != b for a, b in zip(oc.data, om.data)),
             "xk_index": sum((a ^ b) & ((1 << ov) - 1) != 0 for a, b in zip(oc.user, om.user)),
             "ovflo": sum((a >> ov & 1) != (b >> ov & 1) for a, b in zip(oc.user, om.user)),
             "other_tuser": sum((a ^ b) >> (ov + 1) != 0 for a, b in zip(oc.user, om.user)),
             "tlast": sum(a != b for a, b in zip(oc.last, om.last)),
             "core_ovflo_samples": sum(a >> ov & 1 for a in oc.user)}
        lat = {"core": [oc.latency_first, oc.latency_last], "model": [om.latency_first, om.latency_last]}
        clocks = {name: [o.first_in, o.last_in, o.first_out, o.last_out] for name, o in (("core", oc), ("model", om))}
        result["frames"].append({"differences": d, "latency": lat, "first_in_last_in_first_out_last_out": clocks})
        dut._log.info("frame %d: latency first/last core %d/%d model %d/%d; differences %s", f, oc.latency_first,
                      oc.latency_last, om.latency_first, om.latency_last, d)
        exact = {k: v for k, v in d.items() if k != "core_ovflo_samples" and not (f == OVERFLOW_FRAME and k == "data")}
        if any(exact.values()):
            failures.append(f"frame {f}: differences {exact}")
        if f == OVERFLOW_FRAME and not d["core_ovflo_samples"]:
            failures.append(f"frame {f} was meant to overflow but did not")
        if f != OVERFLOW_FRAME:
            xfft_tb.check_frame(cfg, frames[f], oc, dut._log, f"core frame {f}")
        for a, b in zip(lat["core"], lat["model"]):
            if abs(a - b) > LATENCY_TOLERANCE:
                failures.append(f"frame {f}: latency core {lat['core']}, model {lat['model']}")
    if [w for _, w in rc.status] != [w for _, w in rm.status]:
        failures.append(f"status words differ: core {rc.status}, model {rm.status}")
    if [c for c, _ in rc.status] != [c for c, _ in rm.status]:
        failures.append(f"status word clocks differ: core {rc.status}, model {rm.status}")
    result["overflow_frame_data_differences"] = result["frames"][OVERFLOW_FRAME]["differences"]["data"]
    result["clocks"] = [rc.clocks, rm.clocks]
    RESULTS.write_text(json.dumps(result, indent=1))
    dut._log.info("status words core %s, model %s; events core %s, model %s", rc.status, rm.status, rc.events,
                  rm.events)
    dut._log.info("overflowing frame %d: %d of %d data words differ (OVFLO identical: %s)", OVERFLOW_FRAME,
                  result["overflow_frame_data_differences"], cfg.points,
                  result["frames"][OVERFLOW_FRAME]["differences"]["ovflo"] == 0)
    assert not failures, "; ".join(failures)


@pytest.mark.parametrize("sim", hdl_sources.simulators(encrypted_ip=True))
def test_xfft_compare(sim: str) -> None:
    cfg = xfft_config.current()
    sim_runner.run(
        sim=sim,
        sources=[hdl_sources.xfft_ip(cfg), hdl_sources.xfft_engine(cfg), Path(__file__).with_name("xfft_pair.sv")],
        hdl_toplevel="xfft_pair",
        hdl_toplevel_lang="verilog",
        test_module=__name__,
        build_name=f"{cfg.tag}/xfft_compare",
        verilog_defines={"XFFT_CORE_NAME": cfg.module, "XFFT_ENGINE_NAME": "xfft_engine_model",
                         "XFFT_MODEL_PKG": cfg.model_package},
        extra_env={"XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
        cache=True,
        cache_outputs=[RESULTS.name],
    )
