"""Example: output backpressure and input gaps, with the data checked against numpy and the timing against the
timing constants. Runs on either model: FFT_MODEL=engine (the stand-in, the C engine: XSim, NVC, GHDL and
Verilator) or FFT_MODEL=rtl (the real core).

    FFT_MODEL=engine pytest examples/cocotb_backpressure        the stand-in: seconds once the core is generated,
                                                                on each simulator installed
    FFT_MODEL=rtl pytest examples/cocotb_backpressure           the same test on the real core (sign-off)

Two cocotb tests, each from its own reset:
- stalls_and_gaps: one frame with input gaps (s_axis_data_tvalid low) and output stalls (m_axis_data_tready
  low), one of them longer than the core's output queue (OUTPUT_HALT_EVENT_AFTER), so that the pipeline pauses.
  The data must be the numpy FFT within a few LSB, and the latencies and the status word's clock must be what
  the timing constants and the rules of model/xfft_timing.py predict;
- random_traffic: four frames back to back with random gaps and stalls: the data of every frame must be right.

The run is cached (docs/CACHE.md): a second run with the same inputs takes seconds and ends with a CachedResult
warning (the "sim_cache: hit" log line shows with pytest -s).
"""

from __future__ import annotations

import os
import random

import cocotb
import numpy as np
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

import hdl_sources
import sim_runner
import xfft_config
import xfft_tb
import xfft_timing
from xfft_tb import Frame, Lane


async def start(dut) -> tuple[xfft_config.XfftConfig, dict, Lane]:
    """Clock, reset, and a Lane (driver and monitor of the core's ports)."""
    cfg = xfft_config.current()
    cocotb.start_soon(Clock(dut.aclk, 4, unit="ns").start())
    lane = Lane(dut, cfg)
    lane.idle()
    dut.aresetn.value = 0
    await ClockCycles(dut.aclk, 50)
    dut.aresetn.value = 1
    return cfg, xfft_timing.constants(cfg), lane


@cocotb.test()
async def stalls_and_gaps(dut):
    """One frame: input gaps of 1 and 4 clocks, output stalls of 3 and 20 clocks in the middle of the frame."""
    cfg, t, lane = await start(dut)
    n = cfg.points
    gaps = {n // 4: 1, n // 2: 4}                   # clocks of tvalid low before input sample k
    stalls = {n // 4: 3, n // 2: 20}                # clocks of tready low before output sample k
    frame = Frame(xfft_tb.random_frame(cfg, np.random.default_rng(3)), True, xfft_tb.schedule_a(cfg), gaps, stalls)
    r = await lane.run(dut.aclk, [frame])
    out = r.frames[0]
    xfft_tb.check_frame(cfg, frame, out, dut._log, "stalls_and_gaps")

    # The input slack rule: a gap the slack absorbs is free, a longer one delays the frame
    cost, _ = xfft_timing.gap_cost([gaps[k] for k in sorted(gaps)], t["INPUT_SLACK"])
    first = t["LATENCY_FIRST_OUT"] + cost
    last = first + n - 1 + t["OUTPUT_STALL_COST"] * sum(stalls.values())
    assert (out.latency_first, out.latency_last) == (first, last), \
        f"latency {out.latency_first}/{out.latency_last}, expected {first}/{last}"
    # The status word: after the last output, earlier by the stall clocks the queue absorbed (queue rule)
    stall_list = [stalls[k] for k in sorted(stalls)]
    status = (t["STATUS_AFTER_LAST_OUT"] - sum(stall_list) * t["OUTPUT_STALL_COST"]
              + xfft_timing.status_delay(stall_list, t))
    assert len(r.status) == 1 and r.status[0][0] - out.last_out == status, f"status word {r.status}"
    dut._log.info("latency %d / %d clocks (constants: LATENCY_FIRST_OUT %d + gap cost %d), status word %d after "
                  "the last output", out.latency_first, out.latency_last, t["LATENCY_FIRST_OUT"], cost, status)


@cocotb.test()
async def random_traffic(dut):
    """Four frames back to back, forward and inverse, with random gaps (1 to 4 clocks) and stalls (1 to 30)."""
    cfg, _, lane = await start(dut)
    rnd, rng, n = random.Random(cocotb.RANDOM_SEED), np.random.default_rng(cocotb.RANDOM_SEED), cfg.points
    frames = [Frame(xfft_tb.random_frame(cfg, rng), f % 2 == 0, xfft_tb.schedule_a(cfg),
                    {k: rnd.randint(1, 4) for k in range(1, n) if rnd.random() < 0.03},
                    {k: rnd.randint(1, 30) for k in range(n) if rnd.random() < 0.03}) for f in range(4)]
    r = await lane.run(dut.aclk, frames)
    for f, (frame, out) in enumerate(zip(frames, r.frames)):
        xfft_tb.check_frame(cfg, frame, out, dut._log, f"frame {f}")
    assert [w for _, w in r.status] == [0] * 4, f"status words {r.status}"


@pytest.mark.parametrize("sim", hdl_sources.simulators())
def test_backpressure(sim: str) -> None:
    cfg = xfft_config.current()
    model = hdl_sources.fft_model()
    sim_runner.run(
        sim=sim,                                          # vivado, nvc or ghdl (engine only)
        sources=[hdl_sources.xfft_core(cfg, model)],      # the real core or the stand-in, by FFT_MODEL
        hdl_toplevel=cfg.module,                          # the core is the toplevel
        hdl_toplevel_lang=hdl_sources.toplevel_lang(model, sim),
        test_module=__name__,
        build_name=f"{cfg.tag}/example_backpressure",
        verilog_defines={"XFFT_CORE_NAME": cfg.module},
        extra_env={"FFT_MODEL": model, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
        cache=True,
    )
