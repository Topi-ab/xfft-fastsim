"""Example: the configuration channel. Each frame takes the s_axis_config word offered before it: FWD_INV (bit 0,
1 = forward) and SCALE_SCH (2 bits per radix-4 stage above it; xfft_config.XfftConfig.config_word()).

    FFT_MODEL=engine pytest examples/config_channel         (XSim, NVC, GHDL, Verilator, where installed)
    FFT_MODEL=rtl pytest examples/config_channel

Two cocotb tests, each from its own reset:
- round_trip: a tone through a forward transform scaled by about 1/N, then its output through an inverse
  transform scaled by 1/2 per stage: the tone comes back, scaled, within a few LSB (the core's inverse is
  unnormalised: N * ifft);
- overflow: full-scale random data with SCALE_SCH 0 overflows: OVFLO is set in m_axis_data_tuser and in the
  status word. After an overflow the stand-in's data differs from the core's (only the flag is exact), so only
  the flag is checked.
"""

from __future__ import annotations

import os

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


async def start(dut) -> tuple[xfft_config.XfftConfig, Lane]:
    cfg = xfft_config.current()
    cocotb.start_soon(Clock(dut.aclk, 4, unit="ns").start())
    lane = Lane(dut, cfg)
    lane.idle()
    dut.aresetn.value = 0
    await ClockCycles(dut.aclk, 50)
    dut.aresetn.value = 1
    return cfg, lane


@cocotb.test()
async def round_trip(dut):
    """Forward with SCALE_SCH 2 per stage, then inverse with SCALE_SCH 1 per stage: the tone comes back."""
    cfg, lane = await start(dut)
    n, k = cfg.points, cfg.points // 8 + 3
    amplitude = 2 ** (cfg.input_width - 3)
    tone = np.round(amplitude * np.exp(2j * np.pi * k * np.arange(n) / n))
    scaled = xfft_tb.schedule_b(cfg)                # 2 per stage (1 in a radix-2 last stage)
    forward = Frame(tone, True, scaled)
    r = await lane.run(dut.aclk, [forward])
    spectrum = np.array([xfft_tb.unpack(cfg, w) for w in r.frames[0].data])
    peak = int(np.argmax(np.abs(spectrum)))
    assert peak == k, f"forward: peak at bin {peak}, expected {k}"
    assert [w for _, w in r.status] == [0], f"forward: status {r.status}"

    halved = [1] * cfg.stages
    inverse = Frame(spectrum, False, halved)
    r = await lane.run(dut.aclk, [inverse])
    back = np.array([xfft_tb.unpack(cfg, w) for w in r.frames[0].data])
    expected = tone * n / 2 ** (sum(scaled) + sum(halved))     # N * ifft(fft(x) / 2^a) / 2^b
    err = float(np.max(np.abs(back - expected)))
    dut._log.info("round trip: tone at bin %d, amplitude %d: largest error %.2f LSB", k, amplitude, err)
    assert err < 8, f"round trip: error {err:.2f} LSB"
    assert [w for _, w in r.status] == [0], f"inverse: status {r.status}"


@cocotb.test()
async def overflow(dut):
    """Full-scale data without scaling: OVFLO in tuser and in the status word."""
    cfg, lane = await start(dut)
    frame = Frame(xfft_tb.random_frame(cfg, np.random.default_rng(5), full_scale=True), True, [0] * cfg.stages)
    r = await lane.run(dut.aclk, [frame])
    flagged = sum(u >> cfg.ovflo_bit & 1 for u in r.frames[0].user)
    dut._log.info("overflow: OVFLO on %d of %d output samples, status word %s", flagged, cfg.points, r.status)
    assert flagged > 0, "OVFLO not set"
    assert len(r.status) == 1 and r.status[0][1] & 1, f"status word {r.status}"


@pytest.mark.parametrize("sim", hdl_sources.simulators())
def test_config_channel(sim: str) -> None:
    cfg = xfft_config.current()
    model = hdl_sources.fft_model()
    sim_runner.run(
        sim=sim,
        sources=[hdl_sources.xfft_core(cfg, model)],
        hdl_toplevel=cfg.module,
        hdl_toplevel_lang=hdl_sources.toplevel_lang(model, sim),
        test_module=__name__,
        build_name=f"{cfg.tag}/example_config_channel",
        verilog_defines={"XFFT_CORE_NAME": cfg.module},
        extra_env={"FFT_MODEL": model, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
        cache=True,
    )
