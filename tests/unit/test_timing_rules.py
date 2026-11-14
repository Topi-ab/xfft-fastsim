"""The input slack rule and the queue and status rules of model/xfft_timing.py against the timing the core has in
the default configuration (1024 points, INPUT_SLACK = 1), the constants table, and the configuration module: no
Vivado."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "model"))

import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402

# Input gaps of one frame (clocks, in input order) -> expected delay of its first output
EXPECTED_GAPS = {(1,): 0, (2,): 2, (3,): 3, (5,): 5, (100,): 100, (1, 1): 2, (1, 5): 6, (5, 1): 5, (1, 2): 3,
                 (2, 1): 2, (1, 1, 1): 2, (1, 5, 1): 6, (1, 1, 5): 7, (5, 1, 1): 7, (1, 1, 1, 1): 4}


@pytest.mark.parametrize("gaps,delay", EXPECTED_GAPS.items())
def test_gap_cost_matches_the_core(gaps, delay):
    assert xfft_timing.gap_cost(list(gaps), 1)[0] == delay


def test_slack_at_the_next_frame():
    # (slack left by frame 0, idle clocks before frame 1) -> frame 1's latency - LATENCY_FIRST_OUT
    expected = {(1, 0): 0, (0, 0): -1, (1, 1): -1, (0, 1): 0, (1, 5): 0, (0, 5): 0}
    for (left, idle), offset in expected.items():
        assert xfft_timing.slack_after_idle(left, idle, 1) - 1 == offset


# Restart rule: (gaps of frame 0, idle clocks, gaps of frame 1) -> frame 1's latency - LATENCY_FIRST_OUT, in the
# default configuration (a frame starting afresh, 1024 points). gaps[k]: clocks before sample k.
EXPECTED_RESTART = [
    ({}, 2, {1: 1}, 2), ({}, 2, {1: 2}, 2), ({}, 2, {2: 1}, 0), ({}, 2, {3: 1}, 0), ({}, 3, {1: 1}, 2),
    ({}, 6, {1: 2}, 2), ({}, 1, {1: 1}, 1), ({}, 0, {1: 1}, 0),
    ({1023: 3}, 1, {1: 1}, 1), ({1023: 3}, 2, {1: 1}, 2), ({1023: 2}, 1, {1: 1}, 1),
]


@pytest.mark.parametrize("gaps0,idle,gaps1,offset", EXPECTED_RESTART)
def test_restart_rule_matches_the_core(gaps0, idle, gaps1, offset):
    _, left, restart = xfft_timing.frame_gap_cost(gaps0, 1024, 1)
    start, restart = xfft_timing.frame_start(left, idle, 1, restart)
    cost = xfft_timing.frame_gap_cost(gaps1, 1024, 1, start, restart)[0]
    assert start - 1 + cost == offset


def test_restart_leaves_more_slack():
    # A 1-clock gap right after a frame's first sample after idle clocks costs S + 1 = 2 and leaves 2: a later
    # 2-clock gap is free
    assert xfft_timing.frame_gap_cost({1: 1, 10: 2}, 1024, 1) == (2, 0, False)
    # Right after the sample a 2-clock gap restarted the pipeline with (it starved), a 1-clock gap costs S + 2 = 3
    # (the characterization case gaps_2_1_d1)
    assert xfft_timing.frame_gap_cost({256: 2, 257: 1}, 1024, 1)[0] == 5
    assert xfft_timing.gap_step(1, 1, 1, 1) == (2, 2, 2)
    assert xfft_timing.gap_step(1, 1, 1, 2) == (3, 3, 2)
    assert xfft_timing.gap_step(1, 1, 1, 0) == (0, 0, 0)
    assert xfft_timing.frame_gap_cost({1: 1, 2: 2}, 1024, 1) == (5, 3, False)
    assert xfft_timing.gap_cost([1], 1) == (0, 0)


# Output stalls in the middle of a frame (clocks, in order) -> expected delay of its status word (the default
# configuration: 1024 points, OUTPUT_HALT_EVENT_AFTER = 14, OUTPUT_BUFFER = 11, PIPELINE_LOOKAHEAD = 3,
# OUTPUT_QUEUE_SIZE = 16)
RULES = {"OUTPUT_HALT_EVENT_AFTER": 14, "OUTPUT_BUFFER": 11, "STATUS_AFTER_LAST_OUT": 1, "PIPELINE_LOOKAHEAD": 3,
         "OUTPUT_QUEUE_SIZE": 16}
EXPECTED_STATUS = {(1,): 0, (2,): 0, (11,): 0, (12,): 0, (13,): 0, (14,): 3, (15,): 4, (20,): 9, (100,): 89, (1, 5): 0}


@pytest.mark.parametrize("stalls,delay", EXPECTED_STATUS.items())
def test_status_delay_matches_the_core(stalls, delay):
    assert xfft_timing.status_delay(list(stalls), RULES) == delay


# One output stall of s clocks before sample N - k of the last frame -> expected delay of its status word
# (the default configuration, k = 14 .. 34)
EXPECTED_END = {**{(k, s): 0 for k in (14, 15) for s in (20,)},
                **{(k, s): 0 for k in (16, 17) for s in (12, 13, 14, 15, 16, 20, 30)},
                (18, 12): 0, (18, 13): 0, (18, 14): 0, (18, 15): 1, (18, 16): 2, (18, 20): 6, (18, 30): 16,
                **{(k, s): s - 11 for k in range(19, 35) for s in (15, 20, 30)},
                **{(k, s): s - 11 for k in (19, 20) for s in (14, 16)}}
# The same in a stream (the next frame right behind): k = 14 .. 24 at 512, 1024 and 2048 points
EXPECTED_END_STREAM = {**{(k, s): 0 for k in (14, 15, 16) for s in (14, 20)},
                       **{(17, s): s - 11 for s in (14, 20)}, **{(18, s): s - 12 for s in (14, 20)},
                       **{(k, s): s - 11 for k in range(19, 25) for s in (14, 20)}}


@pytest.mark.parametrize("stall,delay", EXPECTED_END.items())
def test_status_delay_at_the_end_of_the_last_frame_matches_the_core(stall, delay):
    assert xfft_timing.status_delay_at(*stall, RULES) == delay


@pytest.mark.parametrize("stall,delay", EXPECTED_END_STREAM.items())
def test_status_delay_at_a_frame_end_in_a_stream_matches_the_core(stall, delay):
    assert xfft_timing.status_delay_at(*stall, RULES, followed=True) == delay


def test_status_clock_without_stalls_is_the_frame_end():
    # with STATUS_AFTER_LAST_OUT = 1 the word comes k clocks after the clock sample N - k was offered
    for k in (5, 17, 30):
        assert xfft_timing.status_clock(k, 0, RULES, False) == k
        assert xfft_timing.status_clock(k, 0, RULES, True) == k


# A costing input gap of a later frame halts the pipeline among a frame's last samples, no output stalls: the
# clocks in which the pipeline starves, counted from the clock it put out sample N - 1 - K -> the clock its status
# word was offered (a starve right after sample N - 1 - K delays the word by one clock more
# than one a clock later)
EXPECTED_STARVING = {(): 5, (3, 4): 7, (4, 5): 6, (1, 2, 3, 4, 5): 10, (2, 3, 4, 5, 6): 10, (3, 4, 5, 6, 7): 10,
                     (4, 5, 6, 7, 8): 9}


@pytest.mark.parametrize("starving,clock", EXPECTED_STARVING.items())
def test_status_word_while_the_pipeline_starves_matches_the_core(starving, clock):
    assert xfft_timing.status_word_clock(0, set(starving).__contains__, lambda t: t not in starving,
                                         RULES) == clock


def test_derived_and_stored_quantities():
    t = xfft_timing.TABLE[xfft_config.XfftConfig().tag]
    assert xfft_timing.pipeline_input_delay(t) == 5
    assert xfft_timing.lookahead(t) == 3
    assert xfft_timing.queue_size(t) == 16


def test_relations_are_evaluable_on_the_table():
    # The relations (xfft_timing.RELATIONS) are observations, never rules: whether they hold is reported by the
    # characterization bench and checked by no test. Here only that each can be evaluated on every TABLE entry.
    for tag, t in xfft_timing.TABLE.items():
        for name, sides in xfft_timing.RELATIONS.items():
            left, right = sides(t)
            assert isinstance(left, int) and isinstance(right, int), (tag, name)


def test_default_lookahead_and_queue_are_stored():
    # K and Q of the default configuration are table entries (what the stall_end sweeps give: stream edge 16,
    # last-frame edge 17), not computed from other constants
    t = xfft_timing.TABLE[xfft_config.XfftConfig().tag]
    assert (xfft_timing.lookahead(t), xfft_timing.queue_size(t)) == (3, 16)


def sweep_edges(t: dict, stall: int = 100, ks=range(1, 41)) -> tuple[int, int]:
    """The largest k of the stall_end sweeps (characterization bench) whose status word is on time, in a stream
    and at the end of the last frame, by the rules; asserts that every smaller k is on time."""
    edges = []
    for followed in (True, False):
        on_time = [k for k in ks if xfft_timing.status_delay_at(k, stall, t, followed) == 0]
        assert on_time == list(range(1, max(on_time) + 1))
        edges.append(max(on_time))
    return edges[0], edges[1]


@pytest.mark.parametrize("a,b,k,q", [(14, 11, 3, 16), (14, 11, 3, 15), (14, 11, 3, 17), (14, 11, 3, 20),
                                     (14, 13, 3, 16), (14, 5, 2, 15), (10, 7, 1, 10), (20, 11, 5, 24),
                                     (30, 22, 3, 32), (6, 2, 4, 7)])
def test_the_stall_end_sweeps_measure_k_and_q(a, b, k, q):
    # what extract.py reads off the sweeps: K = stream edge - (A - 1); Q = end edge - 1 for
    # A + K - 1 <= Q <= A + K (a larger queue never limits: the same as A + K; a smaller one ends where the
    # stream does)
    t = dict(RULES, OUTPUT_HALT_EVENT_AFTER=a, OUTPUT_BUFFER=b, PIPELINE_LOOKAHEAD=k, OUTPUT_QUEUE_SIZE=q)
    stream, end = sweep_edges(t)
    assert stream - (a - 1) == k
    assert end == max(a + k - 1, min(q, a + k) + 1)


def test_every_table_entry_has_every_constant():
    for tag, values in xfft_timing.TABLE.items():
        assert set(values) == set(xfft_timing.NAMES), tag
    cfg = xfft_config.XfftConfig()
    package = xfft_timing.sv_package(cfg, xfft_timing.TABLE[cfg.tag])
    for name in ("OUTPUT_BUFFER", "INPUT_RESUME", "PIPELINE_LOOKAHEAD", "OUTPUT_QUEUE_SIZE"):
        assert f"localparam int XFFT_{name} = " in package
    for name in ("STATUS_STALL_DELAY", "XFFT_LOOKAHEAD", "XFFT_QUEUE ", "PIPELINE_INPUT_DELAY"):
        assert name not in package


def test_no_slack_means_every_gap_costs_its_length():
    assert xfft_timing.gap_cost([1, 1, 3], 0) == (5, 0)


def test_config_spec_round_trip_and_widths():
    cfg = xfft_config.parse("nfft=9,input_width=18,rounding=truncation")
    assert xfft_config.parse(cfg.spec) == cfg
    assert (cfg.points, cfg.stages, cfg.config_width, cfg.data_width, cfg.user_width, cfg.ovflo_bit) == \
        (512, 5, 16, 48, 24, 16)
    assert cfg.max_scale(4) == 1                    # radix-2 last stage of an odd nfft
    assert xfft_config.XfftConfig().spec == ""
    with pytest.raises(ValueError):
        xfft_config.parse("scaling=unscaled")
    with pytest.raises(ValueError):
        xfft_config.parse("points=1024")


def test_builder_tcl_is_self_contained_and_asks_for_the_c_model():
    tcl = xfft_config.XfftConfig().builder_tcl()
    assert "proc create_xfft_core" in tcl and "generate_target cmodel" in tcl
    assert str(xfft_config.REPO) not in tcl         # no machine path: the cache key holds its contents
