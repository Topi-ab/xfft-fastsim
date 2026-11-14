"""The variations of the characterization bench: one short scenario each (1 to 4 frames), described by the input
gaps and output stalls the test bench's Lane (tests/xfft_tb.py) applies. Shared by the bench, the extractor
and the comparison.

gaps[f][k]: clocks of s_axis_data_tvalid low before input sample k of frame f (k = 0, f > 0: idle clocks
between frames f - 1 and f). stalls[f][k]: clocks of m_axis_data_tready low before output sample k of frame f.

Input data: all zeros, unless the case sets full_scale. The core's handshakes do not depend on the data, and
zeros simulate the encrypted core faster. A case that needs an overflow on purpose (the OVFLO bit of tuser and
of the status word) sets full_scale: full-scale random data (xfft_tb.random_frame(cfg, rng, full_scale=True)).
The output checksums of compare.py then compare zeros on the other cases, which still checks that both models
give out the same words. The OVFLO path itself is exercised by tests/test_xfft_compare.py (SCALE_SCH 0).

Three kinds of case:
- pinning cases (cases(n)): the extraction derives every constant from them (extract.py) and both models are
  compared on them (compare.py);
- known approximations (known="<reason>"): variations on which the stand-in is known to differ from the real
  core (docs/USER_GUIDE.md, "Known limitations"). compare.py lists their differences apart (only in the input
  handshakes, by a few clocks; any other difference fails) and says so when one becomes identical; the
  extraction does not use them;
- extra comparison cases (extra=True, cases(n, extra=True), python -m characterize --extra): combinations of
  the phenomena the pinning cases measure one at a time (gaps and stalls together, several stalls per frame,
  stalls at the first and last samples, a blocked output released at odd points, starvation of other periods
  and lengths while earlier frames stream out, idles between frames). The extraction does not use them; they
  are only compared. Several of them combine input gaps with a paused pipeline, where the stand-in is known to
  take the input a few samples earlier than the core (docs/USER_GUIDE.md, "Known limitations"), so they can
  fail the comparison.

The positions of the cases are for N = 256 and up. At N <= 192, halt_dense, stall_then_halt,
short_stall_then_halt, halt_boundary and halt_tail give no evidence, and the refusal cases may not fill the input
FIFO; extract.py reports each such case as a problem. The stall_end_idle positions are tied to
OUTPUT_HALT_EVENT_AFTER = 14 and PIPELINE_LOOKAHEAD = 3.

The stall_end sweeps (END_SWEEP, END_STALL): one stall of END_STALL clocks before sample N - k of frame 0, for
every k of END_SWEEP, once in a stream (stall_end_stream_<k>_<s>, the next frame behind it) and once at the end of
the last frame (stall_end_<k>_<s>). K = PIPELINE_LOOKAHEAD and Q = OUTPUT_QUEUE_SIZE are read off the sweeps
relative to A = OUTPUT_HALT_EVENT_AFTER (K is the largest k on time in a stream less A - 1, Q follows from the
largest k on time at the end of the last frame and A + K: extract.py). By the queue and status rules
(model/xfft_timing.py) the status word is on time up to k = A + K - 1 in a stream and up to
k = max(A + K - 1, min(Q, A + K) + 1) at the end of the last frame, and delayed beyond: a paused pipeline puts out
at most K more samples, so a queue of A + K or more never limits it and reads as A + K, and a queue below
A + K - 1 cannot be read. The stall_<s> set (1, 2, 13, 14, 15, 20, 100) pins A only at 2, 14 or 15: for any other
A, A is reported as a problem and K and Q cannot be read; such a core needs a wider stall_<s> set. The k sweep
brackets both boundaries when A + K + 1 < 40. The stall is as long as the longest of the stall_<s> cases, so it
exceeds every pause threshold A they can measure. In a stream the input FIFO is not full: frame 1 is all in long
before frame 0's last samples come out; what sets the stream apart from the end of the last frame is that frame
1's samples are already in the pipeline behind frame 0's last ones, so within the lookahead the paused pipeline
has no clock without a sample due.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Everything the bench builds and runs, and its results (out/<tag>.json); gitignored
BUILD = REPO / "build" / "characterize"
OUT = BUILD / "out"
# The real core and the stand-in (the C engine)
MODELS = ("rtl", "engine")
# The stall_end sweeps (module docstring): the stall's positions k (before sample N - k) and its length
END_SWEEP = range(1, 41)
END_STALL = 100


@dataclass(frozen=True)
class Case:
    name: str
    frames: int = 1
    gaps: dict = field(default_factory=dict)
    stalls: dict = field(default_factory=dict)
    purpose: str = ""
    full_scale: bool = False        # full-scale random input data instead of zeros (to overflow on purpose)
    extra: bool = False             # an extra comparison case: compared only, never used by the extraction
    known: str = ""                 # a known approximation of the stand-in: why (compared, does not fail)


def cases(n: int, extra: bool = False) -> dict[str, Case]:
    """The pinning cases and the known approximations at N = n points; with extra, also the extra comparison
    cases."""
    q, h = n // 4, n // 2
    # 1-clock gaps of frame 2 after the stall of frame 0 in the *stall_then_halt cases: at least two (the first
    # one is free) while frame 0 streams out, also at N = 256
    halt_step = 64 if n >= 512 else 32
    out = [
        Case("ideal", purpose="LATENCY_FIRST_OUT, LATENCY_LAST_OUT, RESET_TO_READY, delays"),
        *[Case(f"gap_{g}", gaps={0: {h: g}}, purpose="single gap: INPUT_SLACK") for g in (1, 2, 3, 5, 100)],
        Case("gaps_1_1_d1", gaps={0: {q: 1, q + 1: 1}}, purpose="gap pair, adjacent samples: slack rule"),
        Case("gaps_1_1_d16", gaps={0: {q: 1, q + 16: 1}}, purpose="gap pair 16 apart: slack rule"),
        Case("gaps_1_1_d256", gaps={0: {q: 1, h: 1}}, purpose="gap pair N/4 apart: slack rule"),
        Case("gaps_1_5", gaps={0: {q: 1, h: 5}}, purpose="slack rule"),
        Case("gaps_5_1", gaps={0: {q: 5, h: 1}}, purpose="slack rule"),
        Case("gaps_1_2", gaps={0: {q: 1, h: 2}}, purpose="slack rule"),
        Case("gaps_2_1", gaps={0: {q: 2, h: 1}}, purpose="slack rule"),
        Case("gaps_1_1_1", gaps={0: {q: 1, h: 1, h + q: 1}}, purpose="slack rule"),
        Case("gaps_1_1_1_1", gaps={0: {q // 2: 1, q: 1, h: 1, h + q: 1}}, purpose="slack rule"),
        *[Case(f"idle_{i}", frames=2, gaps={1: {0: i}}, purpose="idle clocks between frames: slack at frame start")
          for i in (1, 2, 5)],
        Case("gaps_1_at_1", gaps={0: {1: 1}},
             purpose="slack rule: a 1-clock gap right after a sample that restarted the pipeline (the frame's first "
                     "after reset) costs S + 1 and leaves S + 1"),
        Case("gaps_2_1_d1", gaps={0: {q: 2, q + 1: 1}},
             purpose="slack rule: a 1-clock gap right after the sample a costing gap restarted the pipeline with"),
        Case("idle_2_gap_1", frames=2, gaps={1: {0: 2, 1: 1}},
             purpose="slack rule: frame 1 starts afresh, a 1-clock gap before its sample 1 costs S + 1"),
        Case("idle_2_gap_1_then_2", frames=2, gaps={1: {0: 2, 1: 1, h: 2}},
             purpose="slack rule: the slack of S + 1 left makes a later 2-clock gap free"),
        Case("idle_1_after_restart", frames=2, gaps={0: {n - 1: 3}, 1: {0: 1, 1: 1}},
             purpose="slack rule: frame 0's last sample restarted the pipeline, 1 idle clock: frame 1 starts with "
                     "S + 1"),
        Case("gap_1_then_back_to_back", frames=2, gaps={0: {h: 1}}, purpose="slack carried into the next frame"),
        Case("gap_1_then_idle_1", frames=2, gaps={0: {h: 1}, 1: {0: 1}}, purpose="slack refilled by idle"),
        *[Case(f"stall_{s}", stalls={0: {h: s}},
               purpose="OUTPUT_STALL_COST, OUTPUT_HALT_EVENT_AFTER, OUTPUT_BUFFER (status), queue rule (13, 14)")
          for s in (1, 2, 13, 14, 15, 20, 100)],
        Case("stalls_1_5", stalls={0: {q: 1, h: 5}}, purpose="OUTPUT_STALL_COST over two stalls"),
        Case("back_to_back", frames=3, purpose="CONFIG_ACCEPT_LOADING, FRAME_BOUNDARY_STALL, latency per frame"),
        Case("starve", frames=2, gaps={1: {16: 3 * n}}, purpose="STARVE_HALTS_OUTPUT"),
        Case("halt_1", frames=3, gaps={2: {k: 1 for k in range(32, n, 64)}},
             purpose="halting by 1-clock gaps: the pipeline input delay"),
        Case("blocked", frames=3, stalls={0: {0: 6 * n}}, purpose="INPUT_LIMIT, INPUT_RESUME, OUTPUT_BUFFER"),
        Case("fifo_backlog", frames=3, stalls={0: {100: 40}}, gaps={2: {400: 1, 600: 5}},
             purpose="input FIFO: gaps after an output stall; INPUT_RESUME, OUTPUT_BUFFER"),
        # Pinning variations of the queue, the input resume and the status rules (xfft_timing.py)
        *[Case(f"release_{k}", frames=3, stalls={0: {1: 100, 1 + k: 50}},
               purpose=f"INPUT_RESUME: a release of {k} samples into a full input FIFO (the shortest release "
                       "that resumes the input, D clocks after the INPUT_RESUME-th sample left; then as many "
                       "samples in as left; a shorter one counts towards the next release)")
          for k in (3, 4, 7, 10)],
        Case("stall_then_halt", frames=3, stalls={0: {16: 20}}, gaps={2: {k: 1 for k in range(192, n, halt_step)}},
             purpose="OUTPUT_BUFFER: the FIFO backlog of a stall absorbs gaps, then the queue (the status lead "
                     "shrinks)"),
        Case("short_stall_then_halt", frames=3, stalls={0: {16: 8}},
             gaps={2: {k: 1 for k in range(192, n, halt_step)}},
             purpose="OUTPUT_BUFFER: 8 queued samples absorb the first starving halts, then the output halts"),
        *[Case(f"stall_end_{k}_{END_STALL}", stalls={0: {n - k: END_STALL}},
               purpose="OUTPUT_QUEUE_SIZE, status rule at the end of the last frame (sweep over k): its last "
                       "samples that fit into the queue come out during a long stall, so the word is on time up "
                       "to k = Q + 1 and delayed beyond (from k = A + K + 1 on as in the middle of the frame)")
          for k in END_SWEEP],
        *[Case(f"stall_end_stream_{k}_{END_STALL}", frames=2, stalls={0: {n - k: END_STALL}},
               purpose="PIPELINE_LOOKAHEAD, status rule in a stream (sweep over k): the status logic sees K "
                       "samples ahead, so a frame whose last sample is within K of the pause is on time (up to "
                       "k = A + K - 1) and the word of a later k waits for the pipeline to run again")
          for k in END_SWEEP],
        Case("stall_end_idle", frames=3, stalls={0: {n - 17: 20}}, gaps={1: {0: 4}},
             purpose="queue rule: before idle clocks a frame's last samples that fit into the queue come out "
                     "during a long stall, as at the end of the last frame (on time)"),
        *[Case(f"stall_end_idle_{i}", frames=3, stalls={0: {n - 17: 20}}, gaps={1: {0: i}},
               purpose="queue rule: after 1 idle clock the slack fills the pipeline's gap between the frames, "
                       "so the pipeline sees the next frame's samples as in a stream (delayed); after 2 it sees "
                       "a clock without a sample, as at the end of the last frame (on time)")
          for i in (1, 2)],
        *[Case(f"dribble_{'_'.join(map(str, rel))}", frames=3,
               stalls={0: {0: 3 * n, **{sum(rel[:i + 1]): 60 if i == len(rel) - 1 else 30
                                        for i in range(len(rel))}}},
               purpose="queue rule: a blocked output released in dribbles restarts the pipeline only once "
                       "A - B (OUTPUT_HALT_EVENT_AFTER - OUTPUT_BUFFER) samples are out (2, 1, 2 and 3, 1: 3 "
                       "samples leave, too few to resume the input; 1, 3: 4, within the second release); input "
                       "resume rule: the input takes the samples that left once INPUT_RESUME did")
          for rel in ((2, 1, 2), (3, 1), (1, 3))],
        Case("halt_dense", frames=3, gaps={2: {k: 1 for k in range(200, 240, 3)}},
             purpose="pipeline input delay: halts 8 clocks apart, shifted in time, not in samples"),
        Case("halt_boundary", frames=4, gaps={3: {k: 2 for k in range(96, 160, 6)}},
             purpose="starving halts on the boundary between two frames' output: data and status tvalid low"),
        Case("halt_tail", frames=9, gaps={f: {k: 2 for k in range(61 + f, 320, 6)} for f in range(3, 9)},
             purpose="status rule: 2-clock gaps every 6 samples, at another phase in each of frames 3 to 8, so "
                     "that whatever the latency the pipeline starves right after it puts out sample N - 1 - K of "
                     "one of frames 0 to 5 (the status word's first step waits for a clock without starving)"),
        dense_stalls_1(n),
    ]
    return {c.name: c for c in out + known_cases(n) + (extra_cases(n) if extra else [])}


def dense_stalls_1(n: int) -> Case:
    """1-clock stalls and gaps at random."""
    rnd = random.Random(5)
    return Case("dense_stalls_1", frames=3,
                gaps={f: {k: 1 for k in range(1, n) if rnd.random() < 1 / 64} for f in (0, 1, 2)},
                stalls={f: {k: 1 for k in range(n) if rnd.random() < 1 / 16} for f in (0, 1, 2)},
                purpose="1-clock stalls (1 in 16 samples) and 1-clock gaps (1 in 64): the queue fills to A "
                        "under dense backpressure")


def known_cases(n: int) -> list[Case]:
    """The known approximations (see the module docstring): dense output backpressure with input gaps."""
    rnd = random.Random(7)
    dense = Case("dense_backpressure", frames=3,
                 gaps={f: {k: rnd.randint(1, 4) for k in range(1, n) if rnd.random() < 0.02} for f in (1, 2)},
                 stalls={f: {k: rnd.randint(1, 8) for k in range(n) if rnd.random() < 0.05} for f in (0, 1, 2)},
                 purpose="random stalls of 1 to 8 clocks (1 in 20 samples) and gaps of 1 to 4 (1 in 50) while "
                         "the input FIFO is nearly full",
                 known="dense output backpressure: input refusals a few clocks off")
    return [dense]


def extra_cases(n: int) -> list[Case]:
    """The extra comparison cases (see the module docstring); positions scale with N from N = 512 up."""
    q, h = n // 4, n // 2
    out = [
        Case("mix_gap_stall_mix", frames=2, gaps={0: {q: 2, h: 1, h + 1: 1}, 1: {q + 3: 3}},
             stalls={0: {q: 5, h + 7: 20}, 1: {h: 1}}, purpose="gaps and stalls in the same frames"),
        Case("mix_stalls_first_mid_last", frames=2, stalls={0: {1: 9, h: 16, n - 1: 7}, 1: {0: 30, n - 1: 40}},
             purpose="stalls before the first, a middle and the last sample of a frame"),
        Case("mix_many_stalls", frames=3,
             stalls={1: {q // 2: 2, q // 2 + 1: 3, q: 14, h: 15, h + 1: 1, h + q: 30, n - 40: 12}},
             purpose="several stalls of mixed lengths in one frame while the next one loads"),
        Case("mix_blocked_release_odd", frames=4, stalls={0: {0: 4 * n, 5: 37, 6: 2, 23: 100, h + 3: 9}},
             purpose="output blocked until the input is full, released for 5, 1 and 17 samples"),
        Case("mix_blocked_mid", frames=3, stalls={0: {h: 3 * n}, 1: {7: 13}}, gaps={2: {h: 3}},
             purpose="output blocked in the middle of a frame until the input is full, then a stall and a gap"),
        Case("mix_starve_p5", frames=3, gaps={2: {k: 1 for k in range(17, n, 5)}},
             purpose="1-clock gaps every 5 samples while frame 0 streams out"),
        Case("mix_starve_mixed", frames=4,
             gaps={1: {h: 40}, 2: {k: 2 for k in range(13, n, 37)}, 3: {k: 4 for k in range(5, n, 100)}},
             purpose="a long gap, then 2-clock gaps every 37 and 4-clock gaps every 100 samples"),
        Case("mix_idles", frames=4, gaps={1: {0: 3}, 2: {0: 50, 10: 1}, 3: {0: 1, 1: 2}},
             purpose="idles of 3, 50 and 1 clocks between frames, gaps right after the frame start"),
        Case("mix_gaps_in_stall", frames=3, stalls={0: {h: 30, h + 40: 14}},
             gaps={2: {k: 3 for k in range(h + 100, h + 260, 9)}},
             purpose="3-clock gaps of the loading frame around and after output stalls of the frame before"),
        Case("mix_end_stalls", frames=2, stalls={0: {n - 20: 15, n - 17: 14, n - 1: 25}, 1: {n - 18: 3, n - 16: 20}},
             purpose="several stalls among the last samples of a frame"),
        Case("mix_stall_chain", frames=2, stalls={0: {q: 13, q + 1: 13, q + 2: 13}, 1: {q: 12, q + 12: 12}},
             purpose="stalls just below the status threshold, one sample apart and 12 apart"),
        Case("mix_full_scale", frames=3, gaps={1: {q: 3}}, stalls={0: {h: 20}, 2: {n - 18: 30}}, full_scale=True,
             purpose="overflowing data (status values) with gaps and stalls"),
        Case("mix_every_frame", frames=4, stalls={f: {(97 * f + 3) % n: 14 + f} for f in range(4)},
             gaps={3: {k: 1 for k in range(h, h + 64, 4)}},
             purpose="a stall in every frame at another phase, 1-clock gaps every 4 samples in the last"),
        Case("mix_blocked_then_starve", frames=4, stalls={0: {0: 3 * n}},
             gaps={2: {q: 6}, 3: {k: 1 for k in range(5, n, 23)}},
             purpose="output blocked until the input is full, then gaps while the backlog drains"),
        Case("mix_drain_between_stalls", frames=3, stalls={0: {q: 14, 3 * q: 9, 3 * q + 40: 6}},
             gaps={2: {k: 2 for k in range(h, n - 64, 50)}},
             purpose="stalls before and after 2-clock gaps of the loading frame that drain the FIFO backlog "
                     "and the queue part way"),
        Case("mix_blocked_dribble", frames=3, stalls={0: {0: 3 * n, 2: 30, 3: 30, 5: 30, 6: 30, 7: 60, 15: 25}},
             gaps={2: {0: 7, 100: 3, h: 2}},
             purpose="output blocked until the input is full, released in dribbles of 2, 1, 2, 1, 1 and 8 "
                     "samples, an idle and gaps around the fill"),
        Case("mix_frame_end_cluster", frames=4,
             stalls={0: {n - 18: 14, n - 2: 6}, 1: {0: 12, 1: 3, n - 19: 13}, 2: {n - 17: 40, n - 1: 2},
                     3: {0: 25}},
             gaps={3: {0: 4}},
             purpose="stalls clustered among the last and first samples of consecutive frames, an idle"),
        Case("mix_starve_while_stalled", frames=4, stalls={0: {h + 50: 25}, 1: {q // 2: 16}},
             gaps={2: {k: 3 for k in range(h - 3, h + q, 7)}, 3: {k: 1 for k in range(q, q + 40, 2)}},
             purpose="3-clock gaps every 7 samples and 1-clock gaps every 2 while earlier frames stall"),
        Case("mix_idle_restart", frames=3, gaps={1: {0: n + 50}, 2: {0: 2 * n + 300, 1: 1, 3: 1}},
             stalls={1: {0: 20}, 2: {0: 3, 5: 15}},
             purpose="idles shorter and longer than the latency, stalls on the first samples after them"),
        Case("mix_blocked_mid_gappy_fill", frames=4,
             stalls={0: {h: 3 * n, h + 3: 11, h + 10: 2, h + 11: 30}, 1: {n - 1: 20}},
             gaps={1: {k: 2 for k in range(q, n, 97)}, 2: {h - 40: 3, h + 100: 2, 3 * q + 50: 4}, 3: {7: 1, 8: 1}},
             purpose="output blocked mid-frame while the input fills with gaps, released for 3, 7 and 1 samples"),
        Case("mix_block_dribble_starve", frames=4, stalls={0: {0: 3 * n, 4: 23, 6: 41, 7: 2, q + 5: 17}, 2: {h: 14}},
             gaps={2: {k: 6 for k in range(9, h, 11)}, 3: {0: 5, h: 300}},
             purpose="output blocked until the input is full, released for 4, 2 and 1 samples while 6-clock gaps "
                     "every 11 samples cross the fill; an idle and a long gap in the last frame"),
        Case("mix_mid_dribble_halts", frames=4,
             stalls={1: {h: 25, h + 1: 25, h + 2: 25, h + 3: 25, h + 5: 25, h + 8: 13, h + 9: 13}},
             gaps={3: {k: 1 for k in range(h, n - 32, 3)}},
             purpose="a mid-frame queue released 1 sample at a time (each below K), then 2, then stalls below "
                     "A, while 1-clock gaps every 3 samples halt the pipeline"),
        Case("mix_threshold_edges", frames=4,
             stalls={0: {0: 13, n - 4: 14}, 1: {1: 15, n - 3: 16, n - 1: 13},
                     2: {q: 11, q + 11: 12, h: 14, h + 1: 1}, 3: {n - 16: 17, n - 2: 3}},
             gaps={2: {0: 2}, 3: {0: 9}},
             purpose="stalls of A - 1 .. A + 3 at the first, middle and last samples of every frame, idles of 2 "
                     "and 9 clocks, a last-frame end stall"),
        Case("mix_starve_phases", frames=4,
             gaps={1: {k: 2 for k in range(7, n, 13)}, 2: {h + 3: h + 5}, 3: {k: 5 for k in range(1, q, 29)}},
             stalls={0: {3 * q: 19}, 1: {h: 3, h + 2: 3, h + 4: 3}, 2: {n - 1: 9}},
             purpose="2-clock gaps every 13, a gap of N/2 + 5 and 5-clock gaps every 29 samples while earlier "
                     "frames stream out with stalls"),
        Case("mix_end_gaps_boundary", frames=3,
             gaps={0: {n - 1: 3, n - 3: 1}, 1: {0: 2, 1: 1, n - 2: 7}, 2: {0: h, 2: 1}},
             stalls={0: {n - 2: 1, n - 1: 15}, 1: {0: 14, n - 4: 20}, 2: {0: 2, n - 1: 30}},
             purpose="input gaps on the last and first samples of frames, output stalls on the last and first "
                     "output samples, an idle of N/2"),
        Case("mix_block_frame_end", frames=4,
             stalls={0: {n - 5: 3 * n, n - 3: 31, n - 2: 1}, 1: {0: 20, 1: 9, 2: 40, q: 14}},
             gaps={3: {0: 3, q: 2, q + 1: 2}},
             purpose="output blocked 5 samples before a frame end until the input is full, released in dribbles "
                     "of 2, 1, 2, 1 and 1 samples across the frame boundary"),
        Case("mix_first_mid_last_gappy", frames=3,
             stalls={0: {0: 25, 1: 2, h: 3, h + 1: 16, n - 2: 4, n - 1: 19}, 1: {0: 14, n - 3: 2},
                     2: {0: 60, h: 1, n - 1: 2}},
             gaps={1: {0: 2, 1: 1, n // 8: 4, h + 1: 2, n - 1: 3}, 2: {0: 1, n - 2: 2}},
             purpose="several stalls on the first, middle and last output samples of every frame, input gaps on "
                     "the first and last input samples"),
        Case("mix_blocked_gappy_fill", frames=4,
             stalls={0: {3 * q: 3 * n, 3 * q + 1: 9, 3 * q + 4: 21, 3 * q + 6: 5, 3 * q + 17: 40}},
             gaps={1: {k: 2 for k in range(3, n, 7)}, 2: {k: 2 for k in range(3, n, 7)}, 3: {1: 5}},
             purpose="output blocked at 3N/4 while the input fills with 2-clock gaps every 7 samples, released "
                     "for 1, 3, 2 and 11 samples"),
        Case("mix_end_multistall_idles", frames=4,
             stalls={0: {n - 6: 2, n - 4: 3, n - 2: 2, n - 1: 5}, 1: {n - 18: 6, n - 12: 9, n - 5: 12},
                     2: {0: 7, n - 17: 16}, 3: {n - 19: 13, n - 16: 13, n - 1: 30}},
             gaps={1: {0: 1}, 2: {0: 3}, 3: {0: 2, 1: 1}},
             purpose="several short stalls among the last samples of every frame, idles of 1, 3 and 2 clocks"),
        Case("mix_starve_phases_stalled", frames=4,
             stalls={0: {q: 12, h: 17, 3 * q: 25}, 1: {n // 8: 8, n - 1: 20}, 2: {h + 5: 3}},
             gaps={1: {k: 3 for k in range(5, h, 11)},
                   2: {**{k: 1 for k in range(q, q + 60, 2)}, **{k: 4 for k in range(h, n, 47)}},
                   3: {k: 2 for k in range(1, n // 8, 3)}},
             purpose="3-clock gaps every 11, 1-clock gaps every 2, 4-clock gaps every 47 and 2-clock gaps every "
                     "3 samples while earlier frames stream out with stalls"),
        Case("mix_periodic_lockstep", frames=3,
             stalls={f: {k: 3 for k in range(5, n, 24)} for f in (0, 1)},
             gaps={f: {k: 2 for k in range(3, n, 10)} for f in (1, 2)},
             purpose="3-clock stalls every 24 output samples and 2-clock gaps every 10 input samples at once"),
        Case("mix_blocked_first_dribble_gaps", frames=4,
             stalls={0: {0: 4 * n, 1: 12, 2: 12, 4: 30, 5: 3, 10: 50, 13: 7}},
             gaps={1: {k: 2 for k in range(h, n, 5)}, 2: {k: 3 for k in range(1, n, 4)}},
             purpose="output blocked until the input is full, released for 1, 1, 2, 1, 5 and 3 samples while the "
                     "input has 2-clock gaps every 5 and 3-clock gaps every 4 samples"),
        Case("mix_idle_bursts", frames=4,
             stalls={0: {n - 1: 40}, 1: {0: 5, 2: 5, 4: 5}, 2: {h: 30}, 3: {0: 1}},
             gaps={1: {0: h}, 2: {0: 7, 2: 2}, 3: {0: q + 3}},
             purpose="idles of N/2, 7 and N/4 + 3 clocks between frames, stalls on the first samples after them"),
        Case("mix_mid_block_short_releases_starve", frames=4,
             stalls={1: {h: 3 * n, h + 2: 15, h + 4: 15, h + 8: 15}},
             gaps={2: {k: 1 for k in range(2, n, 2)}, 3: {k: 2 for k in range(q, h, 9)}},
             purpose="output blocked mid-frame until the input is full, released for 2, 2 and 4 samples with "
                     "15-clock stalls, while 1-clock gaps every 2 samples starve the pipeline"),
        Case("mix_first_sample_stalls_gappy", frames=3,
             stalls={0: {0: 17, 1: 1, 2: 4}, 1: {0: 3, 1: 22, 2: 2}, 2: {0: 13, 1: 13, 3: 40}},
             gaps={0: {1: 2, 2: 1}, 1: {0: 1, 2: 3}, 2: {0: 6, 1: 1, 2: 1}},
             purpose="several stalls on the first output samples of every frame, gaps on the first input samples "
                     "and idles of 1 and 6 clocks"),
        Case("mix_mid_multistall_gaps", frames=3,
             stalls={0: {h - 3: 4, h: 9, h + 2: 1, h + 5: 18, h + 30: 2},
                     1: {q: 6, q + 1: 6, q + 2: 6, q + 3: 6, q + 4: 14}},
             gaps={1: {k: 1 for k in range(h - 20, h + 40, 6)}, 2: {q - 2: 4, q: 4, q + 2: 4, h: 1}},
             purpose="clustered mid-frame stalls of 1 to 18 clocks while the loading frame has 1-clock gaps every "
                     "6 samples, then 4-clock gaps 2 samples apart"),
        Case("mix_last_samples_stream", frames=4,
             stalls={0: {n - 1: 16}, 1: {n - 2: 9, n - 1: 9}, 2: {n - 3: 15, n - 1: 1}, 3: {n - 5: 3, n - 1: 50}},
             gaps={0: {n - 2: 2}, 1: {0: 4, n - 1: 1}, 2: {0: 1, n - 3: 3}, 3: {0: 2, n - 1: 5}},
             purpose="stalls on the last output samples and gaps on the last input samples of every frame, "
                     "idles of 4, 1 and 2 clocks"),
        Case("mix_blocked_odd_release_gappy", frames=4,
             stalls={0: {n // 8 + 1: 4 * n, n // 8 + 4: 19, n // 8 + 5: 7, n // 8 + 12: 33, n // 8 + 13: 1,
                         h + 7: 45}},
             gaps={1: {k: 3 for k in range(11, n, 17)}, 2: {k: 1 for k in range(2, n, 3)}, 3: {5: 9}},
             purpose="output blocked at N/8 + 1 until the input is full, released for 3, 1, 7 and 1 samples while "
                     "the input has 3-clock gaps every 17 and 1-clock gaps every 3 samples"),
        Case("mix_blocked_last_release_idle", frames=5,
             stalls={1: {n - 1: 3 * n}, 2: {0: 5, 1: 5, 3: 27, q + 1: 3}},
             gaps={2: {0: 11, q: 2}, 3: {0: 1, 1: 1, h: 4}, 4: {k: 2 for k in range(1, n, 9)}},
             purpose="output blocked before the last sample of frame 1 until the input is full (in frame 4, with "
                     "2-clock gaps every 9 samples), released for 1, 1, 2 and N/4 - 2 samples across the frame "
                     "boundary, idles of 11 and 1 clocks"),
        Case("mix_starve_p7_p3", frames=4,
             gaps={1: {k: 2 for k in range(4, n, 7)}, 2: {k: 1 for k in range(1, n, 3)},
                   3: {k: 6 for k in range(h, n, 19)}},
             stalls={0: {n - 30: 10}, 2: {q: 5}, 3: {h + 1: 2}},
             purpose="2-clock gaps every 7, 1-clock gaps every 3 and 6-clock gaps every 19 samples while earlier "
                     "frames stream out with stalls"),
        Case("mix_idle_mix_end_stalls", frames=4,
             gaps={1: {0: q - 1}, 2: {0: 3, 2: 1}, 3: {0: n + 7}},
             stalls={0: {n - 16: 12}, 1: {0: 2, n - 17: 20}, 2: {n - 1: 8}, 3: {0: 4, n - 2: 14}},
             purpose="idles of N/4 - 1, 3 and N + 7 clocks between frames with stalls among the last samples "
                     "before them and on the first samples after them"),
        Case("mix_long_gap_dense_stalls", frames=3,
             gaps={1: {h: 2 * n + 11}, 2: {1: 7}},
             stalls={0: {k: 2 for k in range(h, n, 13)}, 1: {k: 1 for k in range(0, n, 5)}, 2: {h - 1: 30}},
             purpose="a gap of 2N + 11 mid-frame while 2-clock stalls every 13 samples drain the frame before, "
                     "then 1-clock stalls every 5 samples"),
    ]
    return [replace(c, extra=True) for c in out]


def run_dir(tag: str, case: str, model: str) -> Path:
    """The case's own run directory (a copy of the model's snapshot, its logs and facts)."""
    return BUILD / tag / f"run_{case}_{model}"


def json_path(tag: str, case: str, model: str) -> Path:
    """Where a run's facts land: the declared cache output in its run directory."""
    return run_dir(tag, case, model) / f"{case}.json"
