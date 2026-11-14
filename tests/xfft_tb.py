"""Test bench parts shared by the cocotb tests of the xfft core: frames, a driver/monitor of one core (Lane) and
the floating-point reference.

A Lane drives the core's config and data inputs and its output tready from one coroutine, one clock at a time,
and logs every handshake with its clock index: the latencies are counted between handshakes, as in the
timing constants (model/xfft_timing.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from cocotb.triggers import RisingEdge

import xfft_config


@dataclass
class Frame:
    """One input frame and how it is offered: gaps[k] clocks of s_axis_data_tvalid low before input sample k,
    stalls[k] clocks of m_axis_data_tready low before output sample k of this frame."""
    x: np.ndarray                   # complex, integer parts
    forward: bool
    schedule: list[int]             # SCALE_SCH per stage
    gaps: dict[int, int] = field(default_factory=dict)
    stalls: dict[int, int] = field(default_factory=dict)


@dataclass
class FrameOut:
    first_in: int = -1
    last_in: int = -1
    first_out: int = -1
    last_out: int = -1
    config_at: int = -1
    data: list[int] = field(default_factory=list)
    user: list[int] = field(default_factory=list)
    last: list[int] = field(default_factory=list)

    @property
    def latency_first(self) -> int:
        return self.first_out - self.first_in

    @property
    def latency_last(self) -> int:
        return self.last_out - self.first_in


@dataclass
class LaneResult:
    frames: list[FrameOut]
    status: list[tuple[int, int]]   # (clock, m_axis_status_tdata) of each status handshake
    started: list[int]              # clocks with event_frame_started high
    events: dict[str, int]          # clocks each event output was high
    clocks: int


EVENTS = ("event_frame_started", "event_tlast_unexpected", "event_tlast_missing", "event_fft_overflow",
          "event_status_channel_halt", "event_data_in_channel_halt", "event_data_out_channel_halt")


def schedule_a(cfg: xfft_config.XfftConfig) -> list[int]:
    """Scaling for random data of |x| < 2^(W-3): 2 in the first half of the stages, 1 in the rest."""
    return [min(2 if s < cfg.stages // 2 else 1, cfg.max_scale(s)) for s in range(cfg.stages)]


def schedule_b(cfg: xfft_config.XfftConfig) -> list[int]:
    """2 in every stage (1 in a radix-2 last stage): divides by about N."""
    return [min(2, cfg.max_scale(s)) for s in range(cfg.stages)]


def random_frame(cfg: xfft_config.XfftConfig, rng: np.random.Generator, full_scale: bool = False) -> np.ndarray:
    """|re|, |im| < 2^(W-3), or full scale (overflows without scaling)."""
    bits = cfg.input_width - (1 if full_scale else 3)
    lim = 1 << bits
    return rng.integers(-lim, lim, cfg.points) + 1j * rng.integers(-lim, lim, cfg.points)


def case_frames(cfg: xfft_config.XfftConfig, case) -> list[Frame]:
    """The frames of a characterization case (characterize/bench/cases.py: frames, gaps, stalls, full_scale), as
    its bench makes them: all-zero data (full-scale random with case.full_scale), forward and inverse alternating,
    both scaling schedules."""
    rng = np.random.default_rng(11)
    data = ((lambda: random_frame(cfg, rng, full_scale=True)) if case.full_scale
            else (lambda: np.zeros(cfg.points, complex)))
    return [Frame(data(), f % 2 == 0, schedule_a(cfg) if f % 3 != 2 else schedule_b(cfg),
                  dict(case.gaps.get(f, {})), dict(case.stalls.get(f, {}))) for f in range(case.frames)]


def to_signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def pack(cfg: xfft_config.XfftConfig, z: complex) -> int:
    half = cfg.data_width // 2
    mask = (1 << half) - 1
    return (int(z.real) & mask) | ((int(z.imag) & mask) << half)


def unpack(cfg: xfft_config.XfftConfig, word: int) -> complex:
    half = cfg.data_width // 2
    return complex(to_signed(word, cfg.input_width), to_signed(word >> half, cfg.input_width))


def reference(cfg: xfft_config.XfftConfig, frame: Frame) -> np.ndarray:
    """Floating-point transform with the core's scaling. The core's inverse is unnormalised: N * ifft."""
    y = np.fft.fft(frame.x) if frame.forward else cfg.points * np.fft.ifft(frame.x)
    return y / 2.0 ** sum(frame.schedule)


def error_lsb(cfg: xfft_config.XfftConfig, frame: Frame, out: FrameOut) -> tuple[float, float]:
    """(rms, max) deviation of the output from the reference, in LSB."""
    y = np.array([unpack(cfg, w) for w in out.data])
    err = np.abs(y - reference(cfg, frame))
    return float(np.sqrt(np.mean(err ** 2))), float(err.max())


def value(signal) -> int:
    """A port's value as an integer; 0 while it is not resolvable (U/X before the core drives it)."""
    v = signal.value
    return int(v) if v.is_resolvable else 0


class Lane:
    """Driver and monitor of one core, its ports found as <prefix><port name>."""

    def __init__(self, dut, cfg: xfft_config.XfftConfig, prefix: str = "") -> None:
        self.cfg = cfg
        self.port = {name: getattr(dut, prefix + name) for name in (
            "s_axis_config_tdata", "s_axis_config_tvalid", "s_axis_config_tready",
            "s_axis_data_tdata", "s_axis_data_tvalid", "s_axis_data_tready", "s_axis_data_tlast",
            "m_axis_data_tdata", "m_axis_data_tuser", "m_axis_data_tvalid", "m_axis_data_tready",
            "m_axis_data_tlast", "m_axis_status_tdata", "m_axis_status_tvalid", "m_axis_status_tready",
            *EVENTS)}

    def idle(self) -> None:
        for name in ("s_axis_config_tvalid", "s_axis_config_tdata", "s_axis_data_tvalid", "s_axis_data_tdata",
                     "s_axis_data_tlast", "m_axis_data_tready"):
            self.port[name].value = 0
        self.port["m_axis_status_tready"].value = 1

    async def run(self, clk, frames: list[Frame], timeout: int | None = None) -> LaneResult:
        """Send the frames back to back as far as the core takes them, each frame's config word offered once the
        frame before it has started, and collect everything until all output and the status words are out."""
        cfg, p = self.cfg, self.port
        n, nf = cfg.points, len(frames)
        words = [[pack(cfg, z) for z in f.x] for f in frames]
        configs = [cfg.config_word(f.forward, f.schedule) for f in frames]
        outs = [FrameOut() for _ in frames]
        status, started, events = [], [], dict.fromkeys(EVENTS, 0)
        timeout = timeout or (nf + 8) * 4 * n
        cfg_sent = in_f = in_k = rx_j = gap_left = stall_left = 0
        gap_done = stall_done = False
        cycle, last_out = 0, -1
        while rx_j < nf * n or cycle <= last_out + 32:
            # Drive this clock's inputs
            cfg_valid = cfg_sent < nf and (cfg_sent == 0 or in_f > cfg_sent - 1
                                           or (in_f == cfg_sent - 1 and in_k > 0))
            p["s_axis_config_tvalid"].value = int(cfg_valid)
            if cfg_valid:
                p["s_axis_config_tdata"].value = configs[cfg_sent]
            valid = 0
            if gap_left > 0:
                gap_left -= 1
            elif in_f < nf and cfg_sent > in_f:
                gap = frames[in_f].gaps.get(in_k, 0)
                if not gap_done and gap > 0:
                    gap_left = gap - 1
                else:
                    valid = 1
                    p["s_axis_data_tdata"].value = words[in_f][in_k]
                    p["s_axis_data_tlast"].value = int(in_k == n - 1)
                gap_done = True
            p["s_axis_data_tvalid"].value = valid
            ready = 1
            if stall_left > 0:
                ready, stall_left = 0, stall_left - 1
            elif rx_j < nf * n:
                stall = frames[rx_j // n].stalls.get(rx_j % n, 0)
                if not stall_done and stall > 0:
                    ready, stall_left = 0, stall - 1
                stall_done = True
            p["m_axis_data_tready"].value = ready

            await RisingEdge(clk)
            # Values sampled at this edge (from before it): the handshakes that took place
            if cfg_valid and value(p["s_axis_config_tready"]):
                outs[cfg_sent].config_at = cycle
                cfg_sent += 1
            if valid and value(p["s_axis_data_tready"]):
                if in_k == 0:
                    outs[in_f].first_in = cycle
                if in_k == n - 1:
                    outs[in_f].last_in = cycle
                    in_f, in_k = in_f + 1, 0
                else:
                    in_k += 1
                gap_done = False
            if ready and value(p["m_axis_data_tvalid"]):
                if rx_j >= nf * n:
                    raise AssertionError("more output samples than input samples")
                o = outs[rx_j // n]
                if rx_j % n == 0:
                    o.first_out = cycle
                o.data.append(value(p["m_axis_data_tdata"]))
                o.user.append(value(p["m_axis_data_tuser"]))
                o.last.append(value(p["m_axis_data_tlast"]))
                if rx_j % n == n - 1:
                    o.last_out = last_out = cycle
                rx_j += 1
                stall_done = False
            if value(p["m_axis_status_tvalid"]):
                status.append((cycle, value(p["m_axis_status_tdata"])))
            for name in EVENTS:
                if value(p[name]):
                    events[name] += 1
                    if name == "event_frame_started":
                        started.append(cycle)
            cycle += 1
            if cycle > timeout:
                raise AssertionError(f"timeout after {cycle} clocks: {in_f} frames + {in_k} samples in, "
                                     f"{rx_j} samples out")
        for name in ("s_axis_config_tvalid", "s_axis_data_tvalid"):
            p[name].value = 0
        return LaneResult(outs, status, started, events, cycle)


def check_frame(cfg: xfft_config.XfftConfig, frame: Frame, out: FrameOut, log, label: str,
                max_rms: float = 2.0, max_err: float = 8.0) -> tuple[float, float]:
    """Natural order, tlast, no overflow, and the values close to the reference."""
    n = cfg.points
    assert [u & ((1 << cfg.ovflo_bit) - 1) for u in out.user] == list(range(n)), f"{label}: not in natural order"
    assert out.last == [0] * (n - 1) + [1], f"{label}: tlast not on the last sample only"
    assert not any(u >> cfg.ovflo_bit & 1 for u in out.user), f"{label}: OVFLO set"
    rms, mx = error_lsb(cfg, frame, out)
    log.info("%s: first in -> first out %d, -> last out %d clocks; error vs. float FFT rms %.2f, max %.2f LSB",
             label, out.latency_first, out.latency_last, rms, mx)
    assert rms < max_rms and mx < max_err, f"{label}: error rms {rms:.2f}, max {mx:.2f} LSB"
    return rms, mx
