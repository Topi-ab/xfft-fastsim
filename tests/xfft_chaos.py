"""Seeded random traffic for the C engine's trace test (test_xfft_trace.py): what the Lane of xfft_tb.py never
does. Output stalls that pause the pipeline at any point of a frame, resets in the middle
of a frame, status channel stalls, config words on every clock with junk in the unused bits, tlast early, late or
missing, output blocks long enough to fill the input FIFO and every result slot, idle runs, tvalid dropped before
its handshake, and X or Z on single inputs for a clock.

Chaos drives all inputs at the falling edge of aclk and reads every output there (their values until the next
rising edge, so the handshakes it counts are those of that edge). It reacts to the core's tready as a source
does, so two cores that answer the same see the same traffic. A trace writer gets one line per falling edge: the
inputs driven before it and every output read there (trace_line()). With the clock low until its first rising
edge and Chaos started before it (test_xfft_trace.start_clock()), line k has the inputs of rising edge k and the
outputs after it; with a clock that falls at time 0 as Chaos starts, line 0 is time 0 and line k + 1 has edge k.

Only values every simulator writes alike are used: 0, 1, X and Z (cocotb on XSim writes U, H, L and W as 0). A
2-state simulator (Verilator) gets two_state_segments(): the same traffic without X and Z.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from cocotb.triggers import FallingEdge
from cocotb.types import Logic, LogicArray

import xfft_config
from xfft_tb import EVENTS

INPUTS = ("aresetn", "s_axis_config_tvalid", "s_axis_data_tvalid", "s_axis_data_tlast", "m_axis_data_tready",
          "m_axis_status_tready", "s_axis_config_tdata", "s_axis_data_tdata")
BITS = INPUTS[:6]
OUTPUTS = ("s_axis_config_tready", "s_axis_data_tready", "m_axis_data_tvalid", "m_axis_data_tlast",
           "m_axis_status_tvalid", "m_axis_data_tdata", "m_axis_data_tuser", "m_axis_status_tdata", *EVENTS)


@dataclass(frozen=True)
class Segment:
    """One stretch of traffic, after a reset of 2 to 5 clocks. Probabilities per clock."""
    name: str
    points: int                     # length in clocks, in units of N
    valid: float = 0.98             # s_axis_data_tvalid
    keep: float = 0.9               # an offered sample stays offered until its handshake
    ready: float = 0.98             # m_axis_data_tready
    block: float = 0.0              # start of an output block of 3N to 5N clocks
    stall: float = 0.0              # start of an output stall of 10 to 120 clocks (pauses the pipeline)
    end_stall: float = 0.0          # per frame: a stall of 10 to 120 clocks before one of its last 40 outputs
    frame_idle: float = 0.0         # per frame: 1 to 60 idle clocks after its last input sample
    status_stall: float = 0.0       # start of a status channel stall of 1 to 50 clocks
    config: float = 0.002           # s_axis_config_tvalid
    reset: float = 0.0              # start of a reset of 1 to 4 clocks
    tlast_error: float = 0.0        # a frame with tlast early, late (missing) or both
    unknown: float = 0.0            # one input X or Z for one clock
    idle: float = 0.0               # start of an idle run of 1 to N/4 clocks


def segments(cfg: xfft_config.XfftConfig) -> list[Segment]:
    return [
        Segment("stream", 10),
        Segment("gappy", 10, valid=0.7, keep=0.5, ready=0.8, status_stall=0.01, config=0.01, tlast_error=0.5),
        Segment("stalls", 12, valid=0.995, ready=1.0, stall=3 / cfg.points, idle=1 / cfg.points),
        Segment("frame_ends", 16, valid=0.97, ready=1.0, end_stall=0.8, frame_idle=0.6),
        Segment("blocked", 16, valid=0.95, ready=0.95, block=1 / (2 * cfg.points), status_stall=0.002),
        Segment("config_storm", 6, valid=0.9, ready=0.9, config=1.0),
        Segment("resets", 10, valid=0.9, ready=0.9, reset=1 / cfg.points, tlast_error=0.3, idle=0.002),
        Segment("unknown", 8, valid=0.9, ready=0.9, status_stall=0.005, config=0.01, unknown=0.02),
        Segment("mixed", 12, valid=0.85, keep=0.7, ready=0.85, block=1 / (8 * cfg.points), status_stall=0.005,
                config=0.02, reset=1 / (4 * cfg.points), tlast_error=0.3, unknown=0.005, idle=0.002),
    ]


def two_state_segments(cfg: xfft_config.XfftConfig) -> list[Segment]:
    """segments() without X and Z: the segment of unknown inputs left out, unknown 0 in the others."""
    return [replace(s, unknown=0.0) for s in segments(cfg) if s.name != "unknown"]


def hexstr(value) -> str:
    """A port value as hex digits ('x' for a digit with a bit that is not 0 or 1), single bits as 0, 1 or x."""
    bits = str(value).lower()
    if len(bits) == 1:
        return bits if bits in "01" else "x"
    bits = bits.rjust((len(bits) + 3) // 4 * 4, "0")
    return "".join("x" if any(b not in "01" for b in bits[i:i + 4]) else f"{int(bits[i:i + 4], 2):x}"
                   for i in range(0, len(bits), 4))


class Chaos:
    """Driver of one core's inputs (ports found as <prefix><port name>)."""

    def __init__(self, dut, cfg: xfft_config.XfftConfig, seed: int, trace=None, prefix: str = "") -> None:
        self.cfg = cfg
        self.rnd = random.Random(seed)
        self.trace = trace
        self.port = {name: getattr(dut, prefix + name) for name in (*INPUTS, *OUTPUTS)}
        self.width = {name: len(self.port[name].value) for name in ("s_axis_config_tdata", "s_axis_data_tdata")}
        self.clock = 0
        # unknown: clocks with an X or Z input; unknown_out: clocks with an output that is not 0 or 1
        self.counts = dict.fromkeys(("data_hs", "out_hs", "status_hs", "config_hs", "resets", "unknown",
                                     "unknown_out"), 0)

    def config_word(self) -> int:
        """A valid config word (FWD_INV, SCALE_SCH within each stage's range) with junk in the unused bits."""
        cfg, rnd = self.cfg, self.rnd
        word = cfg.config_word(rnd.random() < 0.5, [rnd.randint(0, cfg.max_scale(s)) for s in range(cfg.stages)])
        used = 1 + 2 * cfg.stages
        return word | rnd.getrandbits(cfg.config_width - used) << used if cfg.config_width > used else word

    def sample(self) -> int:
        """A random data word: full-scale re and im (they overflow with a small SCALE_SCH)."""
        half = self.cfg.data_width // 2
        return self.rnd.getrandbits(half) | self.rnd.getrandbits(half) << half

    async def run(self, clk, segs: list[Segment]) -> dict:
        """Drive the segments one after another; the handshake counts."""
        cfg, rnd, p = self.cfg, self.rnd, self.port
        n = cfg.points
        drive = {"aresetn": 0, "s_axis_config_tvalid": 0, "s_axis_data_tvalid": 0, "s_axis_data_tlast": 0,
                 "m_axis_data_tready": 0, "m_axis_status_tready": 1, "s_axis_config_tdata": 0,
                 "s_axis_data_tdata": 0}
        self.write(drive)
        clean = drive                           # this clock's inputs without the X or Z of unknown()
        ready_before = {"s_axis_config_tready": "0", "s_axis_data_tready": "0", "m_axis_data_tvalid": "x",
                        "m_axis_status_tvalid": "x"}
        in_k = out_k = 0                        # driver's view of the sample indexes in the frame, in and out
        stall_at = None                         # the output index of this frame's end stall
        tlast_at = {n - 1}                      # sample indexes this frame sends tlast with
        word, offered = 0, False
        cfg_word = self.config_word()
        for seg in segs:
            reset_left = rnd.randint(2, 5)
            block_left = stall_left = idle_left = 0
            for _ in range(seg.points * n):
                await FallingEdge(clk)
                out = {name: hexstr(p[name].value) for name in OUTPUTS}
                self.counts["unknown_out"] += any("x" in v for v in out.values())
                # The handshakes of the edge before: the inputs driven for it, the outputs before it
                hs_data = drive["s_axis_data_tvalid"] == 1 and ready_before["s_axis_data_tready"] == "1"
                hs_cfg = drive["s_axis_config_tvalid"] == 1 and ready_before["s_axis_config_tready"] == "1"
                hs_out = drive["m_axis_data_tready"] == 1 and ready_before["m_axis_data_tvalid"] == "1"
                hs_status = drive["m_axis_status_tready"] == 1 and ready_before["m_axis_status_tvalid"] == "1"
                if self.trace is not None:
                    self.trace.write(trace_line(self.clock, drive, out, self.width))
                self.clock += 1
                self.counts["data_hs"] += hs_data
                self.counts["config_hs"] += hs_cfg
                self.counts["out_hs"] += hs_out
                self.counts["status_hs"] += hs_status
                if drive["aresetn"] == 0:
                    in_k, out_k, offered = 0, 0, False
                elif hs_data:
                    offered = False
                    in_k = (in_k + 1) % n
                    if in_k == 0:
                        if rnd.random() < seg.frame_idle:
                            idle_left = rnd.randint(1, 60)
                        tlast_at = {n - 1}
                        if rnd.random() < seg.tlast_error:
                            tlast_at = rnd.choice([set(), {rnd.randrange(n - 1)}, {rnd.randrange(n - 1), n - 1}])
                if hs_out and drive["aresetn"] != 0:
                    out_k = (out_k + 1) % n
                    if out_k == 0:
                        stall_at = n - rnd.randint(1, 40) if rnd.random() < seg.end_stall else None
                if hs_cfg:
                    cfg_word = self.config_word()
                ready_before = {k: out[k] for k in ready_before}

                # This clock's inputs
                drive = dict(clean)
                if reset_left == 0 and rnd.random() < seg.reset:
                    reset_left = rnd.randint(1, 4)
                    self.counts["resets"] += 1
                drive["aresetn"] = 0 if reset_left > 0 else 1
                reset_left = max(0, reset_left - 1)
                drive["s_axis_config_tvalid"] = int(rnd.random() < seg.config)
                if hs_cfg or rnd.random() < 0.1:
                    drive["s_axis_config_tdata"] = cfg_word
                if idle_left == 0 and rnd.random() < seg.idle:
                    idle_left = rnd.randint(1, n // 4)
                if idle_left > 0:
                    idle_left -= 1
                    valid = 0
                else:
                    valid = int(offered and rnd.random() < seg.keep or rnd.random() < seg.valid)
                if valid and not offered:
                    word, offered = self.sample(), True
                if not valid:
                    offered = offered and rnd.random() < seg.keep
                drive["s_axis_data_tvalid"] = valid
                drive["s_axis_data_tdata"] = word if valid else rnd.getrandbits(self.width["s_axis_data_tdata"])
                drive["s_axis_data_tlast"] = int(in_k in tlast_at) if valid else rnd.randint(0, 1)
                if block_left == 0 and rnd.random() < seg.block:
                    block_left = rnd.randint(3 * n, 5 * n)
                if block_left == 0 and (rnd.random() < seg.stall or out_k == stall_at):
                    block_left = rnd.randint(10, 120)
                    stall_at = None if out_k == stall_at else stall_at
                if block_left > 0:
                    block_left -= 1
                    drive["m_axis_data_tready"] = 0
                else:
                    drive["m_axis_data_tready"] = int(rnd.random() < seg.ready)
                if stall_left == 0 and rnd.random() < seg.status_stall:
                    stall_left = rnd.randint(1, 50)
                if stall_left > 0:
                    stall_left -= 1
                    drive["m_axis_status_tready"] = 0
                else:
                    drive["m_axis_status_tready"] = 1
                clean = drive
                if rnd.random() < seg.unknown:
                    drive = self.unknown(drive)
                self.write(drive)
        return dict(self.counts, clocks=self.clock)

    def unknown(self, drive: dict) -> dict:
        """One input X or Z for this clock: a single bit, or some bits of a data or config word."""
        self.counts["unknown"] += 1
        name = self.rnd.choice(INPUTS)
        value = self.rnd.choice("xz")
        if name in BITS:
            return {**drive, name: value}
        width = self.width[name]
        bits = list(f"{drive[name]:0{width}b}")
        for i in self.rnd.sample(range(width), self.rnd.randint(1, min(width, 8))):
            bits[i] = value
        return {**drive, name: "".join(bits)}

    def write(self, drive: dict) -> None:
        for name, v in drive.items():
            if isinstance(v, str):
                self.port[name].value = LogicArray(v.upper()) if name not in BITS else Logic(v.upper())
            else:
                self.port[name].value = v


def trace_line(clock: int, drive: dict, out: dict, widths: dict) -> str:
    """Clock; the six input bits; config and data tdata; the five output bits; m tdata, tuser, status tdata; the
    seven events (hex, x for a digit with an unknown bit)."""
    def word(name: str) -> str:
        v = drive[name]
        return hexstr(v) if isinstance(v, str) else f"{v:0{(widths[name] + 3) // 4}x}"

    return " ".join([str(clock), "".join(hexstr(drive[b]) if isinstance(drive[b], str) else str(drive[b])
                                         for b in BITS),
                     word("s_axis_config_tdata"), word("s_axis_data_tdata"),
                     "".join(out[o] for o in OUTPUTS[:5]), *(out[o] for o in OUTPUTS[5:8]),
                     "".join(out[e] for e in EVENTS)]) + "\n"
