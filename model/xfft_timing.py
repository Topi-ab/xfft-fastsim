"""Cycle timing of the xfft core: the single source of the stand-in's timing.

The IP carries no machine-readable latency (the .xci, the .xml and the C model's API have none), so the numbers
are measured on the real core in XSim: by the characterization bench (python -m characterize, docs/
CHARACTERIZATION.md) or by the timing bench (tests/timing/xfft_timing_bench.py measure, which also checks them:
... validate). They depend on the configuration (model/xfft_config.py), so they are kept per configuration tag.

Where constants() finds them, in this order:
- XFFT_TIMING_JSON, when set: that file (as timing/<tag>.json below), whatever the configuration;
- TABLE below (the configurations that ship with the repository);
- timing/<tag>.json in the repository: the JSON the characterization writes (its "constants"), or a flat
  {NAME: value} dict;
- build/<tag>/timing_measured.json, written by `xfft_timing_bench.py measure` (flat);
- for a module other than xfft_core with none of these under its own tag: the same, under the tag of the same
  configuration with module xfft_core (the module's name does not change the timing: lookup_configs()).

Users:
- the stand-in's C engine (model/xfft_engine.c) takes all its timing from the C header of c_header(), its
  wrappers their widths from the SV package of sv_package() and the VHDL package of vhdl_package()
  (scripts/build_engine.py), all three made from model_values();
- the tests compare the latencies they see with these numbers;
- the timing bench and the characterization bench check the real core (or the stand-in) against them.

Clock counts are between AXI4-Stream handshakes (edges where tvalid and tready are both high), counted from the
first accepted input sample of a frame unless stated otherwise.

    LATENCY_FIRST_OUT       first input handshake -> first output handshake of the same frame, tvalid and
                            tready high throughout, the frame starting with a full input slack
    LATENCY_LAST_OUT        first input handshake -> last output handshake (tlast); LATENCY_FIRST_OUT + N - 1,
                            the frame streams out at one sample per clock
    INPUT_SLACK             the input slack S, a skid of S samples between the input and the pipeline (rule
                            below): a lone 1-clock input gap is free when S = 1
    OUTPUT_STALL_COST       clocks of delay per clock of m_axis_data_tready low
    RESET_TO_READY          first edge with aresetn high -> first edge with s_axis_data_tready and
                            s_axis_config_tready high
    CONFIG_ACCEPT_LOADING   clocks from s_axis_config_tvalid to its handshake while a frame loads
    FRAME_BOUNDARY_STALL    clocks of s_axis_data_tready low between two frames sent back to back
    STARVE_HALTS_OUTPUT     1: the cost of an input gap (rule below) also holds up the output of the frames
                            loaded earlier, clock for clock
    OUTPUT_HALT_EVENT_AFTER A, the number of samples waiting behind the output register at which the pipeline
                            pauses (queue rule below): a stall of A clocks in the middle of a frame is the
                            shortest that delays its status word; event_data_out_channel_halt is high for the
                            clocks of an output stall beyond these
    FRAME_STARTED_DELAY     first input handshake -> event_frame_started; FRAME_STARTED_DELAY - INPUT_SLACK is
                            the pipeline input delay D (rule below)
    STATUS_AFTER_LAST_OUT   last output handshake -> m_axis_status_tvalid (no output stalls)
    INPUT_LIMIT             with the output blocked, samples the core accepts beyond those it has output
    OUTPUT_BUFFER           B, the samples waiting behind the output register at which a paused pipeline
                            resumes (queue rule below): a stall of s >= A clocks in the middle of a frame delays
                            its status word by s - B, and the later status words come B clocks earlier against
                            their frames' last output handshakes, until a starving pipeline drains the queue
    INPUT_RESUME            R, once s_axis_data_tready fell for a full input FIFO, the samples that must leave the
                            FIFO for the pipeline before it rises again (rule below): after a blocked output is
                            released, the input resumes R + (A - B) + (D - K) clocks after the first output
                            handshake, and a release of fewer than R samples does not resume it
    PIPELINE_LOOKAHEAD      K, the samples the pipeline and the status logic see ahead (queue and status rules
                            below): a paused pipeline still runs when one of its next K + 1 clocks has no sample
                            due, a frame's status word starts on its way when the pipeline puts out sample
                            N - 1 - K, and the input side sees a sample leave the input FIFO D - K clocks late
    OUTPUT_QUEUE_SIZE       Q, the samples the queue holds behind the output register (queue rule below): while
                            paused the pipeline puts out a frame's last samples only when they fit into the queue

Derived, not stored: D = FRAME_STARTED_DELAY - INPUT_SLACK (pipeline_input_delay()), the clocks from an input
handshake to the pipeline's input FIFO (rules below): event_frame_started marks the frame's first sample entering
the pipeline.

Input slack rule (gap_step(), gap_cost(), frame_gap_cost(), frame_start()): a gap is a run of L clocks in which a
started frame gets no sample while the core is ready. With slack s left, a gap with L <= s is free and leaves
s - L; a longer gap costs L + S - s clocks of delay (for the frame and, with STARVE_HALTS_OUTPUT, for all output
pending) and refills the slack to S. A frame starts with the slack left by the frame before it, drained by the
idle clocks I between them: with 1 + s - I <= 0 the skid ran empty and the frame starts afresh with S, else with
s - I. It comes out at LATENCY_FIRST_OUT - (S - s) after its first input, plus its gap costs. Restart: the sample
after a gap that costs, and the first sample of a frame that starts afresh, restart the pipeline. A gap right
after a sample that restarted it and exactly as long as the slack left (the sample arrives as the pipeline takes
the restarting one and empties the FIFO) is not free: it costs S + r and leaves S + r, r = 1 after a frame's
first sample and r = 2 after a gap that cost (the pipeline starved before it), and the sample after it restarts
the pipeline again (r = 2: it starved); I = s idle clocks after a last sample that restarted it likewise start
the next frame with S + 1, its first sample restarting it with r = 1.

Pipeline input delay: an accepted sample reaches the pipeline's input FIFO D = FRAME_STARTED_DELAY - INPUT_SLACK
clocks after its handshake (the pulse marks the frame's first sample entering the pipeline, and it comes earlier
with less slack, as the latency does). A gap that costs, with slack s left before it and first gap clock g, halts
the output from clock g + s + 1 + D, for the gap's cost (the pipeline takes a sample one clock after it reached
the FIFO, S clocks later still after it arrived into an empty FIFO while the pipeline waited, and S + 1 clocks
later when it arrived in the clock in which the restarting pipeline took the FIFO's last sample, S + 2 when the
pipeline starved in the clock before that and the sample belongs to the same frame: the restart rule above): the
output register belongs to the pipeline's clock, so m_axis_data_tvalid is low in every clock in which the
pipeline starves, unless samples are queued behind it (queue rule)
or it already offers a sample the sink has not taken (AXI4-Stream: an offered word stays offered until its
handshake). m_axis_status_tvalid is low in those clocks
too (status rule).

Queue rule: the pipeline runs one clock at a time or halts as a whole; behind the output register it queues the
samples it has put out. It pauses when A = OUTPUT_HALT_EVENT_AFTER samples wait behind the register (counted at
the clock, before its output handshake) and resumes when only B = OUTPUT_BUFFER wait. So a stall of s clocks in
the middle of a frame leaves s samples queued when s < A; a longer one halts the pipeline for s - B clocks (the
clocks beyond A during the stall, then A - B after it) and leaves B queued. The input goes on during the halt, so
the input FIFO holds max(0, s - B) samples more after a stall of s >= A: the gap clocks of a loading frame use
them up first, before its gaps cost by the slack rule; a pipeline that starves drains the queue, one sample per
clock, and the output halts only once the queue is empty. A blocked output restarts the pipeline only once A - B
samples have left the full queue, in one release or over several; while the output moves the pipeline runs on,
and when it stalls again the pipeline takes back as many samples as left. The pipeline sees K =
PIPELINE_LOOKAHEAD samples ahead: while paused it still runs when one of its next K + 1 clocks has no sample due
(the end of the last frame, or a frame followed by idle clocks that the input slack does not fill) and the r <= K
samples due before that clock fit into the queue, waiting + r <= Q = OUTPUT_QUEUE_SIZE: then a frame's last
samples come out of the pipeline during the stall.

Input resume rule: the input side counts the samples in the input FIFO (with those still on their way to it): an
accepted sample at once, a sample that left for the pipeline D - K clocks late. s_axis_data_tready falls when that
count reaches the FIFO's size, and rises again once it is R below it. So after a full FIFO the input resumes
D - K clocks after the R-th sample left, and then takes as many samples as have left: after a blocked output, the
pipeline restarts A - B clocks after the first output handshake of the release (queue rule) and the input resumes
R + (A - B) + (D - K) clocks after it; a release of k >= R samples lets k in, a shorter release lets none in but
counts towards the next one; a release of fewer than A - B samples does not restart the pipeline at all. Not
modelled: when the queue pauses the pipeline while input gaps arrive (at some phases of the gap pattern against
the pause), the core's input FIFO can hold a few samples less for that pause and refuses the input that many
samples earlier (docs/USER_GUIDE.md, "Known limitations").

Status rule (status_word_clock()): the status logic sees the frame's end K samples ahead. When the pipeline puts
out the frame's sample N - 1 - K, the status word starts on its way: from the next clock on it waits for a clock
in which the pipeline does not starve, then (from the clock after it) for one in which the pipeline runs (it is
not paused and does not starve), then for one in which it does not starve, and it is offered
STATUS_AFTER_LAST_OUT + 1 clocks after that, in a clock in which the pipeline does not starve
(m_axis_status_tvalid is low while it starves, unless it
already offers a word the sink has not taken). Without halts that is STATUS_AFTER_LAST_OUT + 3 - K clocks after
the frame's last output handshake. A starving halt of L clocks that begins 1 to K clocks after the clock that put
out sample N - 1 - K delays the word by L; one that begins right after the clock that put out sample N - 1 delays
it by L - 1, so the word comes with that sample's handshake (tlast). So a stall of s >= A clocks in the middle of
a frame delays the word by s - B (status_delay()), and one among the frame's last samples by less or not at all
(status_delay_at()): in a stream, a frame whose last sample is within K of the pause is not delayed; at the end of
the last frame (or before idle clocks) also the samples that fit into the queue come out during the stall.

RELATIONS below lists consistency checks between the constants; the characterization bench reports each of them
for a configuration (they are reported, never assumed).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xfft_config  # noqa: E402

TIMING_DIR = xfft_config.REPO / "timing"

NAMES = ("LATENCY_FIRST_OUT", "LATENCY_LAST_OUT", "INPUT_SLACK", "OUTPUT_STALL_COST", "RESET_TO_READY",
         "CONFIG_ACCEPT_LOADING", "FRAME_BOUNDARY_STALL", "STARVE_HALTS_OUTPUT", "OUTPUT_HALT_EVENT_AFTER",
         "FRAME_STARTED_DELAY", "STATUS_AFTER_LAST_OUT", "INPUT_LIMIT", "OUTPUT_BUFFER", "INPUT_RESUME",
         "PIPELINE_LOOKAHEAD", "OUTPUT_QUEUE_SIZE")

# Measured on the real core in XSim. A new version of the core (another Vivado) can have other values: measure
# them again (docs/CHARACTERIZATION.md).
TABLE: dict[str, dict[str, int]] = {
    # The default configuration (xfft_config.XfftConfig()): 1024 points, 16-bit data and phase factors,
    # convergent rounding, xc7a35tcpg236-1, 100 MHz.
    "n1024_d16_t16_conv_xc7a35tcpg236-1_100mhz": {
        "LATENCY_FIRST_OUT": 2182,
        "LATENCY_LAST_OUT": 3205,
        "INPUT_SLACK": 1,
        "OUTPUT_STALL_COST": 1,
        "RESET_TO_READY": 3,
        "CONFIG_ACCEPT_LOADING": 0,
        "FRAME_BOUNDARY_STALL": 0,
        "STARVE_HALTS_OUTPUT": 1,
        "OUTPUT_HALT_EVENT_AFTER": 14,
        "FRAME_STARTED_DELAY": 6,
        "STATUS_AFTER_LAST_OUT": 1,
        "INPUT_LIMIT": 2210,
        "OUTPUT_BUFFER": 11,
        "INPUT_RESUME": 4,
        "PIPELINE_LOOKAHEAD": 3,
        "OUTPUT_QUEUE_SIZE": 16,
    },
}


def gap_step(length: int, slack_size: int, slack: int, restart: int = 0) -> tuple[int, int, int]:
    """(delay, slack left, how the sample after it restarts the pipeline) of one input gap of `length` clocks,
    with `slack` left before it. restart: how the sample before it restarted the pipeline, 0 not at all, 1 as a
    frame's first sample after the skid ran empty, 2 after starving inside the frame (the input slack rule)."""
    if length < slack or (length == slack and not restart):
        return 0, slack - length, 0
    if length == slack:
        return slack_size + restart, slack_size + restart, 2
    return length + slack_size - slack, slack_size, 2


def gap_cost(gaps: list[int], slack_size: int, slack: int | None = None) -> tuple[int, int]:
    """(delay, slack left) of a frame's input gaps of the given lengths, in order, none of them right after a
    sample that restarted the pipeline (the input slack rule; frame_gap_cost() for any gaps)."""
    s = slack_size if slack is None else slack
    cost = 0
    for length in gaps:
        step, s, _ = gap_step(length, slack_size, s)
        cost += step
    return cost, s


def frame_gap_cost(gaps: dict[int, int], n: int, slack_size: int, slack: int | None = None,
                   restart: int = 1) -> tuple[int, int, bool]:
    """(delay, slack left, whether the frame's last sample restarted the pipeline) of a frame's input gaps,
    gaps[k] clocks before its sample k (k >= 1), the frame starting with `slack` (default: afresh) and its first
    sample restarting the pipeline as gap_step() says (default: afresh) (the input slack rule)."""
    s = slack_size if slack is None else slack
    cost, kind, restarted = 0, restart, 0 if restart else -1    # the last sample that restarted the pipeline
    for k in sorted(gaps):
        if 0 < k < n and gaps[k] > 0:
            step, s, after = gap_step(gaps[k], slack_size, s, kind if restarted == k - 1 else 0)
            cost += step
            if after:
                kind, restarted = after, k
    return cost, s, restarted == n - 1


def pipeline_input_delay(timing: dict[str, int]) -> int:
    """D: clocks from an input handshake to the pipeline's input FIFO (derived, see the module docstring)."""
    return timing["FRAME_STARTED_DELAY"] - timing["INPUT_SLACK"]


# Consistency checks between the constants: name -> (left side, right side). Nothing in the model assumes them;
# the characterization bench reports for each whether it holds for the configuration.
RELATIONS = {
    "PIPELINE_LOOKAHEAD = OUTPUT_HALT_EVENT_AFTER - OUTPUT_BUFFER (K = A - B)":
        lambda t: (t["PIPELINE_LOOKAHEAD"], t["OUTPUT_HALT_EVENT_AFTER"] - t["OUTPUT_BUFFER"]),
    "PIPELINE_LOOKAHEAD = D - 2":
        lambda t: (t["PIPELINE_LOOKAHEAD"], pipeline_input_delay(t) - 2),
    "PIPELINE_LOOKAHEAD = INPUT_RESUME - 1":
        lambda t: (t["PIPELINE_LOOKAHEAD"], t["INPUT_RESUME"] - 1),
    "OUTPUT_QUEUE_SIZE = OUTPUT_HALT_EVENT_AFTER + PIPELINE_LOOKAHEAD - 1 (Q = A + K - 1)":
        lambda t: (t["OUTPUT_QUEUE_SIZE"], t["OUTPUT_HALT_EVENT_AFTER"] + t["PIPELINE_LOOKAHEAD"] - 1),
    "OUTPUT_QUEUE_SIZE = OUTPUT_HALT_EVENT_AFTER + 2":
        lambda t: (t["OUTPUT_QUEUE_SIZE"], t["OUTPUT_HALT_EVENT_AFTER"] + 2),
    "OUTPUT_QUEUE_SIZE = 2 OUTPUT_HALT_EVENT_AFTER - OUTPUT_BUFFER - 1":
        lambda t: (t["OUTPUT_QUEUE_SIZE"], 2 * t["OUTPUT_HALT_EVENT_AFTER"] - t["OUTPUT_BUFFER"] - 1),
    "INPUT_RESUME = D - 1":
        lambda t: (t["INPUT_RESUME"], pipeline_input_delay(t) - 1),
    "D - PIPELINE_LOOKAHEAD = OUTPUT_QUEUE_SIZE - OUTPUT_HALT_EVENT_AFTER (the input side's lag)":
        lambda t: (pipeline_input_delay(t) - t["PIPELINE_LOOKAHEAD"],
                   t["OUTPUT_QUEUE_SIZE"] - t["OUTPUT_HALT_EVENT_AFTER"]),
}


def lookahead(timing: dict[str, int]) -> int:
    """K = PIPELINE_LOOKAHEAD: the samples the pipeline and the status logic see ahead (module docstring)."""
    return timing["PIPELINE_LOOKAHEAD"]


def queue_size(timing: dict[str, int]) -> int:
    """Q = OUTPUT_QUEUE_SIZE: the samples the queue holds behind the output register (module docstring)."""
    return timing["OUTPUT_QUEUE_SIZE"]


def status_delay(stalls: list[int], timing: dict[str, int]) -> int:
    """Clocks by which output stalls of the given lengths (in order) delay a frame's status word, the stalls in
    the middle of the frame: apart from each other and from the frame's end (the queue and status rules;
    status_delay_at() for one stall at any position)."""
    lead, delay = 0, 0
    for length in stalls:
        lead += length
        if lead >= timing["OUTPUT_HALT_EVENT_AFTER"]:
            delay += lead - timing["OUTPUT_BUFFER"]
            lead = timing["OUTPUT_BUFFER"]
    return delay


def status_word_clock(put: int, starving, running, timing: dict[str, int]) -> int:
    """The clock at which a frame's status word is offered (the status rule), given the clock `put` at which the
    pipeline put out the frame's sample N - 1 - K, and whether the pipeline starves (starving(t)) and runs
    (running(t): neither paused nor starving) in clock t."""
    t = put + 1
    while starving(t):                          # a clock without starving
        t += 1
    t += 1
    while not running(t):                       # a running pipeline clock
        t += 1
    t += 1
    while starving(t):                          # again a clock without starving
        t += 1
    t += 1 + timing["STATUS_AFTER_LAST_OUT"]
    while starving(t):                          # offered in a clock without starving
        t += 1
    return t


def status_clock(from_end: int, length: int, timing: dict[str, int], followed: bool, waiting: int = 0,
                 gap: int = 0) -> int:
    """The clock at which a frame's status word is offered, counted from the first clock of an output stall of
    `length` clocks before the frame's sample N - from_end, the output streaming before and after it with
    `waiting` samples queued behind the register (the queue and status rules, clock by clock, no starving).
    followed: the next frame's samples follow the last one in the pipeline, after `gap` pipeline clocks without
    a sample (0: a stream; idle clocks between the frames), else nothing does (the last frame)."""
    a, b, k, q = (timing["OUTPUT_HALT_EVENT_AFTER"], timing["OUTPUT_BUFFER"], lookahead(timing),
                  queue_size(timing))
    t = -(from_end + k + 4)
    queued = waiting + 1                        # the output register and the samples behind it
    nxt = from_end - t - waiting - 1            # the next sample the pipeline puts out, as N - nxt

    def sample(i: int) -> bool:
        """A sample is due i pipeline clocks from now."""
        return i < nxt or (followed and i >= max(nxt, 0) + gap)

    paused, put, ran = False, None, {}
    while put is None or not any(ran.get(u) for u in range(put + 2, t)):
        wait = queued - 1
        paused = wait >= a or (paused and wait > b)
        due = 0
        while due <= k and sample(due):
            due += 1
        ran[t] = run = not paused or (due <= k and wait + due <= q)
        if (t < 0 or t >= length) and queued > 0:
            queued -= 1
        if run:
            if sample(0):
                queued += 1
                if nxt == k + 1:
                    put = t
            if nxt > 0:
                nxt -= 1
            elif followed and gap > 0:
                gap -= 1
        t += 1
    return status_word_clock(put, lambda u: False, lambda u: ran[u], timing)


def status_delay_at(from_end: int, length: int, timing: dict[str, int], followed: bool = False,
                    gap: int = 0) -> int:
    """Clocks by which one output stall of `length` clocks before the frame's sample N - from_end delays the
    frame's status word (the frame's only stall, nothing queued before it; status_clock())."""
    return (status_clock(from_end, length, timing, followed, gap=gap)
            - status_clock(from_end, 0, timing, followed, gap=gap))


def frame_start(slack: int, idle: int, slack_size: int, restart: bool = False) -> tuple[int, int]:
    """(slack, how its first sample restarts the pipeline: gap_step()) a frame starts with, after `idle` idle
    clocks since the frame before left `slack`; restart: the last sample of the frame before restarted the
    pipeline (the input slack rule)."""
    if restart and idle == slack:
        return slack_size + 1, 1
    left = 1 + slack - idle
    return (slack_size, 1) if left <= 0 else (left - 1, 0)


def slack_after_idle(slack: int, idle: int, slack_size: int, restart: bool = False) -> int:
    """The slack a frame starts with, after `idle` idle clocks since the frame before left `slack`
    (frame_start())."""
    return frame_start(slack, idle, slack_size, restart)[0]


def measured_file(cfg: xfft_config.XfftConfig) -> Path:
    """Where `xfft_timing_bench.py measure` writes the constants of a configuration that is not in TABLE."""
    return cfg.build_dir / "timing_measured.json"


def timing_file(cfg: xfft_config.XfftConfig) -> Path:
    """Where a characterization result of a configuration is picked up: timing/<tag>.json in the repository."""
    return TIMING_DIR / f"{cfg.tag}.json"


def override_file() -> Path | None:
    """XFFT_TIMING_JSON: a file with the constants to use whatever the configuration, or None."""
    value = os.environ.get("XFFT_TIMING_JSON", "").strip()
    return Path(value) if value else None


def lookup_configs(cfg: xfft_config.XfftConfig) -> list[xfft_config.XfftConfig]:
    """The configurations whose constants are those of cfg, in the order they are looked up: cfg, then for a
    module other than xfft_core the same configuration with module xfft_core (the name does not change the
    timing)."""
    default = dataclasses.replace(cfg, module=xfft_config.DEFAULT_MODULE)
    return [cfg] if default == cfg else [cfg, default]


def table_tag(cfg: xfft_config.XfftConfig) -> str | None:
    """The TABLE entry constants() takes for cfg (its own tag, or that of lookup_configs()), or None: none, or a
    file comes first (XFFT_TIMING_JSON, or a file under cfg's own tag)."""
    if override_file() is not None:
        return None
    for c in lookup_configs(cfg):
        if c.tag in TABLE:
            return c.tag
        if timing_file(c).is_file() or measured_file(c).is_file():
            return None
    return None


def source_file(cfg: xfft_config.XfftConfig) -> Path | None:
    """The file constants() reads (XFFT_TIMING_JSON, or for a configuration that is not in TABLE), or None."""
    override = override_file()
    if override is not None:
        if not override.is_file():
            raise FileNotFoundError(f"XFFT_TIMING_JSON={override}: no such file")
        return override
    for c in lookup_configs(cfg):
        if c.tag in TABLE:
            return None
        for path in (timing_file(c), measured_file(c)):
            if path.is_file():
                return path
    return None


def constants(cfg: xfft_config.XfftConfig | None = None) -> dict[str, int]:
    """The timing constants of a configuration: from TABLE, else from timing/<tag>.json, else from a measurement
    in the build directory, for a module other than xfft_core under its own tag first (module docstring)."""
    cfg = cfg or xfft_config.current()
    tag = table_tag(cfg)
    if tag is not None:
        return dict(TABLE[tag])
    path = source_file(cfg)
    if path is not None:
        values = json.loads(path.read_text())
        values = values.get("constants", values)     # a characterization result, or a flat dict
        missing = set(NAMES) - set(values)
        if missing:
            raise ValueError(f"{path}: missing {', '.join(sorted(missing))}; measure again")
        return {name: int(values[name]) for name in NAMES}
    raise LookupError(f"no timing constants for {cfg.tag}: characterize the core with 'python -m characterize' "
                      f"(XFFT_CONFIG={cfg.spec}) and put its JSON into {TIMING_DIR.name}/, see "
                      f"docs/CHARACTERIZATION.md")


def model_values(cfg: xfft_config.XfftConfig, timing: dict[str, int] | None) -> dict[str, int]:
    """The configuration and, when known, the timing constants, by their names without the XFFT_ prefix: the
    contents of the generated SV package, VHDL package and C header (sv_package(), vhdl_package(), c_header())."""
    values = {
        "N": cfg.points,
        "NFFT": cfg.nfft,
        "STAGES": cfg.stages,
        "INPUT_WIDTH": cfg.input_width,
        "TWIDDLE_WIDTH": cfg.twiddle_width,
        "CONVERGENT": int(cfg.rounding == "convergent"),
        "CONFIG_WIDTH": cfg.config_width,
        "DATA_WIDTH": cfg.data_width,
        "USER_WIDTH": cfg.user_width,
        "OVFLO_BIT": cfg.ovflo_bit,
        "HAS_TIMING": int(timing is not None),
    }
    return values | {name: (timing or {}).get(name, 0) for name in NAMES}


def sv_package(cfg: xfft_config.XfftConfig, timing: dict[str, int] | None) -> str:
    """SV package cfg.model_package (xfft_model_pkg for module xfft_core): the configuration and, when known, the
    timing constants (XFFT_<NAME>)."""
    lines = [f"// Generated by model/xfft_timing.py for {cfg.tag}; do not edit.",
             f"package {cfg.model_package};"]
    lines += [f"    localparam int XFFT_{name} = {value};" for name, value in model_values(cfg, timing).items()]
    lines.append("endpackage")
    return "\n".join(lines) + "\n"


def vhdl_package(cfg: xfft_config.XfftConfig, timing: dict[str, int] | None) -> str:
    """VHDL package cfg.model_package: the same constants as sv_package(), for the VHDL wrapper of the C engine."""
    lines = [f"-- Generated by model/xfft_timing.py for {cfg.tag}; do not edit.",
             f"package {cfg.model_package} is"]
    lines += [f"    constant XFFT_{name}: integer := {value};" for name, value in model_values(cfg, timing).items()]
    lines.append("end;")
    return "\n".join(lines) + "\n"


def c_header(cfg: xfft_config.XfftConfig, timing: dict[str, int] | None) -> str:
    """C header xfft_model.h: the same constants as sv_package(), for the C engine (model/xfft_engine.c)."""
    lines = [f"// Generated by model/xfft_timing.py for {cfg.tag}; do not edit.",
             "#ifndef XFFT_MODEL_H", "#define XFFT_MODEL_H"]
    lines += [f"#define XFFT_{name} {value}" for name, value in model_values(cfg, timing).items()]
    lines.append("#endif")
    return "\n".join(lines) + "\n"
