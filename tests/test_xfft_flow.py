"""Latency and flow control of the bare xfft core, on either model (FFT_MODEL=rtl|engine) and simulator
(XSim; NVC, GHDL and Verilator for the C engine).

The core is the toplevel. Each case starts from its own reset:
- ideal: one forward frame, s_axis_data_tvalid and m_axis_data_tready high throughout;
- valid_gaps: the same frame with s_axis_data_tvalid low for 1 clock at sample N/4 and for 5 at N/2;
- ready_gaps: m_axis_data_tready low for 1 clock at output N/4 and for 5 at N/2;
- both_gaps: both in the same frame;
- back_to_back: 4 frames, forward and inverse with two scaling schedules, sent without a pause;
- random_flow: 6 frames with random input gaps and output stalls (seeded by cocotb's seed).
Every frame is checked against numpy's FFT with the core's scaling; the frames of the first four cases must
also be bit-identical to each other (gaps and stalls must not change values). The latencies are checked
against the timing constants (model/xfft_timing.py) where these predict them; random_flow logs them.
Results (latencies, errors) go to xfft_flow_results.json in the test directory.
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
import xfft_timing
from xfft_tb import Frame, Lane

PERIOD_NS = 4                                   # simulation clock; only clock counts matter
RESULTS = Path("xfft_flow_results.json")        # in the test directory
# Outputs of the single-frame cases: the same frame, so they must be equal
SINGLE: dict[str, list[int]] = {}


def setup() -> tuple[xfft_config.XfftConfig, dict]:
    cfg = xfft_config.current()
    return cfg, xfft_timing.constants(cfg)


async def reset(dut, lane: Lane) -> None:
    lane.idle()
    dut.aresetn.value = 0
    await ClockCycles(dut.aclk, 50)             # longer than glbl's 100 ns power-on reset
    dut.aresetn.value = 1


def save(name: str, result: dict) -> None:
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    results[name] = result
    RESULTS.write_text(json.dumps(results, indent=1))


async def single_frame(dut, name: str, gaps: dict[int, int], stalls: dict[int, int]) -> None:
    cfg, t = setup()
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    lane = Lane(dut, cfg)
    await reset(dut, lane)
    frame = Frame(xfft_tb.random_frame(cfg, np.random.default_rng(1)), True, xfft_tb.schedule_a(cfg), gaps, stalls)
    r = await lane.run(dut.aclk, [frame])
    out = r.frames[0]
    rms, mx = xfft_tb.check_frame(cfg, frame, out, dut._log, name)
    # The input slack rule (model/xfft_timing.py): the gaps in input order
    cost, _ = xfft_timing.gap_cost([gaps[k] for k in sorted(gaps)], t["INPUT_SLACK"])
    expect_first = t["LATENCY_FIRST_OUT"] + cost
    expect_last = expect_first + cfg.points - 1 + t["OUTPUT_STALL_COST"] * sum(stalls.values())
    assert out.latency_first == expect_first, f"{name}: first output after {out.latency_first}, expected {expect_first}"
    assert out.latency_last == expect_last, f"{name}: last output after {out.latency_last}, expected {expect_last}"
    assert len(r.status) == 1 and r.status[0][1] == 0, f"{name}: status words {r.status}"
    # The status word: STATUS_AFTER_LAST_OUT after the last output, earlier by the stall clocks the status
    # rule did not wait for (model/xfft_timing.py, queue and status rules; the stalls here are mid-frame)
    stall_list = [stalls[k] for k in sorted(stalls)]
    expect_status = (t["STATUS_AFTER_LAST_OUT"] - sum(stall_list) * t["OUTPUT_STALL_COST"]
                     + xfft_timing.status_delay(stall_list, t))
    assert r.status[0][0] - out.last_out == expect_status, f"{name}: status at {r.status}, expected {expect_status}"
    assert r.started == [out.first_in + t["FRAME_STARTED_DELAY"]], f"{name}: event_frame_started at {r.started}"
    SINGLE[name] = out.data
    if "ideal" in SINGLE:
        assert out.data == SINGLE["ideal"], f"{name}: output differs from the ideal case"
    dut._log.info("%s: halt events in %d, out %d clocks", name, r.events["event_data_in_channel_halt"],
                  r.events["event_data_out_channel_halt"])
    save(name, {"latency_first": out.latency_first, "latency_last": out.latency_last, "err_rms_lsb": rms,
                "err_max_lsb": mx, "events": r.events, "clocks": r.clocks})


@cocotb.test()
async def ideal(dut):
    """One frame, valid and ready high throughout."""
    RESULTS.unlink(missing_ok=True)
    await single_frame(dut, "ideal", {}, {})


@cocotb.test()
async def valid_gaps(dut):
    """s_axis_data_tvalid low for 1 clock at input sample N/4 and for 5 clocks at N/2."""
    n = xfft_config.current().points
    await single_frame(dut, "valid_gaps", {n // 4: 1, n // 2: 5}, {})


@cocotb.test()
async def ready_gaps(dut):
    """m_axis_data_tready low for 1 clock at output sample N/4 and for 5 clocks at N/2."""
    n = xfft_config.current().points
    await single_frame(dut, "ready_gaps", {}, {n // 4: 1, n // 2: 5})


@cocotb.test()
async def both_gaps(dut):
    """The gaps of valid_gaps and the stalls of ready_gaps in the same frame."""
    n = xfft_config.current().points
    await single_frame(dut, "both_gaps", {n // 4: 1, n // 2: 5}, {n // 4: 1, n // 2: 5})


@cocotb.test()
async def back_to_back(dut):
    """4 frames without a pause: forward / inverse, two scaling schedules."""
    cfg, t = setup()
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    lane = Lane(dut, cfg)
    await reset(dut, lane)
    rng = np.random.default_rng(2)
    frames = [Frame(xfft_tb.random_frame(cfg, rng), f % 2 == 0,
                    xfft_tb.schedule_a(cfg) if f < 2 else xfft_tb.schedule_b(cfg)) for f in range(4)]
    r = await lane.run(dut.aclk, frames)
    for f, (frame, out) in enumerate(zip(frames, r.frames)):
        xfft_tb.check_frame(cfg, frame, out, dut._log, f"back_to_back frame {f}")
        assert out.latency_first == t["LATENCY_FIRST_OUT"], f"frame {f}: first output after {out.latency_first}"
        assert out.latency_last == t["LATENCY_LAST_OUT"], f"frame {f}: last output after {out.latency_last}"
        if f > 0:
            gap = out.first_in - r.frames[f - 1].last_in - 1
            assert gap == t["FRAME_BOUNDARY_STALL"], f"frame {f}: {gap} clocks after the frame before"
    assert [w for _, w in r.status] == [0] * 4
    save("back_to_back", {"latency_first": [o.latency_first for o in r.frames],
                          "latency_last": [o.latency_last for o in r.frames], "clocks": r.clocks})


@cocotb.test()
async def random_flow(dut):
    """6 frames back to back with random input gaps (1 to 4 clocks, about 1 in 50 samples) and output stalls (1
    to 8 clocks, about 1 in 20 samples). The values must be right; the latencies are logged."""
    cfg, _ = setup()
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    lane = Lane(dut, cfg)
    await reset(dut, lane)
    rnd = random.Random(cocotb.RANDOM_SEED)
    rng = np.random.default_rng(cocotb.RANDOM_SEED)
    n = cfg.points
    frames = []
    for f in range(6):
        gaps = {k: rnd.randint(1, 4) for k in range(1, n) if rnd.random() < 0.02}
        stalls = {k: rnd.randint(1, 8) for k in range(n) if rnd.random() < 0.05}
        frames.append(Frame(xfft_tb.random_frame(cfg, rng), f % 2 == 0,
                            xfft_tb.schedule_a(cfg) if f % 3 else xfft_tb.schedule_b(cfg), gaps, stalls))
    r = await lane.run(dut.aclk, frames)
    for f, (frame, out) in enumerate(zip(frames, r.frames)):
        xfft_tb.check_frame(cfg, frame, out, dut._log, f"random_flow frame {f} ({len(frame.gaps)} gaps, "
                                                        f"{len(frame.stalls)} stalls)")
    assert [w for _, w in r.status] == [0] * 6
    save("random_flow", {"seed": cocotb.RANDOM_SEED, "latency_first": [o.latency_first for o in r.frames],
                         "latency_last": [o.latency_last for o in r.frames], "events": r.events,
                         "clocks": r.clocks})


@pytest.mark.parametrize("sim", hdl_sources.simulators())
def test_xfft_flow(sim: str) -> None:
    cfg = xfft_config.current()
    model = hdl_sources.fft_model()
    sim_runner.run(
        sim=sim,
        sources=[hdl_sources.xfft_core(cfg, model)],
        hdl_toplevel=cfg.module,
        hdl_toplevel_lang=hdl_sources.toplevel_lang(model, sim),
        test_module=__name__,
        build_name=f"{cfg.tag}/xfft_flow",
        verilog_defines={"XFFT_CORE_NAME": cfg.module},
        extra_env={"FFT_MODEL": model, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
        # Every input is in the key (fixed frames, the seed of a cached run is fixed); the latencies are the
        # stored output
        cache=True,
        cache_outputs=[RESULTS.name],
    )
