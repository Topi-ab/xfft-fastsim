"""Derives the timing constants of the current configuration (XFFT_CONFIG) from the rtl facts of the
characterization bench (test_characterize.py), each with an explicit formula, and cross-checks them: every case
that carries evidence for a constant must agree, and the input-slack and halting rules must explain every gap
pattern; the queue, the input resume and the status rules (model/xfft_timing.py) every stall pattern;
FRAME_STARTED_DELAY - INPUT_SLACK the clock at which a starving input halts the output, the clocks in which the
output and status channels wait for a starving pipeline, and the delay of the input's resume; the queue the
starving clocks after an output stall. PIPELINE_LOOKAHEAD (K) and OUTPUT_QUEUE_SIZE (Q) come from the stall_end
sweeps (cases.END_SWEEP: a long stall before every k-th last sample, in a stream and at the end of the last frame),
whose boundary between the status words on time and delayed each sweep must bracket; they place the status word
of a stall among a frame's last samples. K and Q are read relative to OUTPUT_HALT_EVENT_AFTER (A), which the
stall_<s> set pins only at 2, 14 or 15; when A is not pinned, K and Q are problems too. The derived D is measured
on its own and checked against its formula. The consistency checks between the constants
(xfft_timing.RELATIONS) are not assumed: they are reported as observations. A pinning case that gives no evidence
at this N is a problem. Writes out/<tag>.json (not when a constant has no evidence; with the problems listed when
there are any), prints the TABLE entry and compares it with TABLE in model/xfft_timing.py. The extra comparison
cases and the known approximations (cases.py) are not used.

    python characterize/bench/extract.py                 (python -m characterize runs it)
    python characterize/bench/extract.py --from engine   the stand-in's facts instead, print only (a self-check:
                                                         they must give back the constants it was built with)

out/<tag>.json is deterministic (no times): the configuration, the core version, the constants, the evidence
per constant, and the provenance (the cache keys of the rtl runs, the extractor version). The engine runs of the
bench build the stand-in from it. Exit code 1 when a fact is missing, the cases disagree, or a variation is not
explained by the constants. A check of an event output that fails is printed as a note and does not fail: the
event outputs are not in the timing scope of the stand-in. The relations are printed as observations and stored
in out/<tag>.json ("relations": each one's two sides); one that does not hold fails nothing.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "model"), str(REPO / "tests")]
import cases  # noqa: E402
import sim_cache  # noqa: E402
import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402

EXTRACTOR_VERSION = 7


class Facts:
    """The facts of one rtl run, with per-frame views."""

    def __init__(self, data: dict) -> None:
        self.d = data
        self.n = data["n"]
        self.frames = data["frames"]

    def first_in(self, f: int) -> int:
        return self.d["in_hs"][f * self.n]

    def last_in(self, f: int) -> int:
        return self.d["in_hs"][f * self.n + self.n - 1]

    def first_out(self, f: int) -> int:
        return self.d["out_hs"][f * self.n]

    def last_out(self, f: int) -> int:
        return self.d["out_hs"][f * self.n + self.n - 1]

    def lat_first(self, f: int = 0) -> int:
        return self.first_out(f) - self.first_in(f)

    def lat_last(self, f: int = 0) -> int:
        return self.last_out(f) - self.first_in(f)

    def gaps(self, f: int) -> list[int]:
        """The input gaps inside frame f, in order: clocks between handshakes beyond one."""
        return [length for _, length in sorted(self.gap_map(f).items())]

    def gap_map(self, f: int) -> dict[int, int]:
        """The input gaps inside frame f: {k: clocks between the handshakes of samples k - 1 and k beyond one}."""
        hs = self.d["in_hs"][f * self.n:(f + 1) * self.n]
        return {k: b - a - 1 for k, (a, b) in enumerate(zip(hs, hs[1:]), 1) if b - a > 1}

    def stalls(self) -> int:
        return len(self.d["out_stalled"])

    def idle(self, f: int) -> int:
        """Clocks between frames f - 1 and f in which no sample was offered (refused offers are not idle)."""
        refused = set(self.d["in_refused"])
        return sum(1 for c in range(self.last_in(f - 1) + 1, self.first_in(f)) if c not in refused)

    def start_slack(self, slack: int) -> list[int]:
        """The input slack each frame starts with (the slack rule of model/xfft_timing.py)."""
        return [start for start, _ in self.starts(slack)]

    def starts(self, slack: int) -> list[tuple[int, int]]:
        """(slack, how its first sample restarts the pipeline: xfft_timing.gap_step()) each frame starts with (the
        slack rule)."""
        out, left, restart = [], slack, True
        for f in range(self.frames):
            start = (slack, 1) if f == 0 else xfft_timing.frame_start(left, self.idle(f), slack, restart)
            out.append(start)
            _, left, restart = xfft_timing.frame_gap_cost(self.gap_map(f), self.n, slack, *start)
        return out


class Extraction:
    def __init__(self, facts: dict[str, Facts]) -> None:
        self.f = facts
        self.evidence: dict[str, list] = {}
        self.problems: list[str] = []
        self.explained: list[str] = []
        self.notes: list[str] = []          # event checks that fail: the events are not in the timing scope
        self.observations: list[str] = []   # facts about the constants that fail nothing (relations, bounds)

    def agree(self, name: str, values: list[tuple[str, int]]) -> int:
        """The value all evidence agrees on; a problem when it does not."""
        self.evidence[name] = [[c, v] for c, v in values]
        distinct = sorted({v for _, v in values})
        if len(distinct) != 1:
            self.problems.append(f"{name}: the cases disagree: {values}")
        return distinct[0] if distinct else -1

    def check(self, what: str, measured: int, expected: int, event: bool = False) -> None:
        """An explained fact, else a problem (a note for an event output's)."""
        if measured == expected:
            self.explained.append(f"{what}: {measured}")
        else:
            (self.notes if event else self.problems).append(
                f"unexplained: {what}: measured {measured}, the constants give {expected}")

    def run(self) -> dict[str, int]:
        f = self.f
        n = next(iter(f.values())).n
        nogap = [c for c in ("ideal", "back_to_back", "idle_5") if c in f]
        lat = self.agree("LATENCY_FIRST_OUT", [(c, x.lat_first(k)) for c in nogap for x in [f[c]]
                                               for k in range(x.frames)]
                         + [(c, f[c].lat_first(0)) for c in f if c.startswith("stall_")])
        lat_last = self.agree("LATENCY_LAST_OUT", [(c, x.lat_last(k)) for c in nogap for x in [f[c]]
                                                   for k in range(x.frames)])
        self.check("LATENCY_LAST_OUT = LATENCY_FIRST_OUT + N - 1", lat_last, lat + n - 1)
        reset = self.agree("RESET_TO_READY",
                           [(c, x.d["data_tready_first"] - x.d["reset_edge"]) for c, x in f.items()]
                           + [(c + " (config)", x.d["cfg_tready_first"] - x.d["reset_edge"]) for c, x in f.items()])

        # Input slack S: the longest run of free single gaps from 1 clock up, every longer one costing its length
        single = {int(c[4:]): f[c].lat_first(0) - lat for c in f if c.startswith("gap_") and c[4:].isdigit()}
        slack = 0
        while slack + 1 in single and single[slack + 1] == 0:
            slack += 1
        self.evidence["INPUT_SLACK"] = [[f"gap_{g}", d] for g, d in sorted(single.items())]
        for g, d in sorted(single.items()):
            self.check(f"gap_{g}: delay of a single {g}-clock gap", d, xfft_timing.gap_cost([g], slack)[0])
        # The slack rule over every pattern of gaps inside a frame (each frame starting afresh after reset)
        # (gaps right after a sample that restarted the pipeline included: gaps_1_at_1, gaps_2_1_d1)
        for c in [c for c in f if c.startswith("gaps_")]:
            gaps = f[c].gap_map(0)
            self.check(f"{c}: delay of gaps {gaps}", f[c].lat_first(0) - lat,
                       xfft_timing.frame_gap_cost(gaps, n, slack)[0])
        # The slack at the start of frame 1: left by frame 0's gaps, drained by the idle clocks between them (or
        # not, after a last sample that restarted the pipeline: idle_1_after_restart); then frame 1's own gaps
        for c in [c for c in f if c.startswith("idle_") or c.startswith("gap_1_then")]:
            x = f[c]
            idle = x.first_in(1) - x.last_in(0) - 1
            start, restart = x.starts(slack)[1]
            cost = xfft_timing.frame_gap_cost(x.gap_map(1), n, slack, start, restart)[0]
            self.check(f"{c}: frame 1 first output after {idle} idle clocks (starts with slack {start}, gaps "
                       f"{x.gap_map(1)})", x.lat_first(1), lat - (slack - start) + cost)
            self.check(f"{c}: frame 1 last output", x.lat_last(1), lat_last - (slack - start) + cost)

        stall_cases = {int(c[6:]): f[c] for c in f if c.startswith("stall_") and c[6:].isdigit()}
        cost = self.agree("OUTPUT_STALL_COST", [(f"stall_{s}", (x.lat_last(0) - lat_last) // s)
                                                for s, x in sorted(stall_cases.items())])
        for s, x in sorted(stall_cases.items()):
            self.check(f"stall_{s}: last output", x.lat_last(0), lat_last + cost * s)
        if "stalls_1_5" in f:
            self.check("stalls_1_5: last output", f["stalls_1_5"].lat_last(0), lat_last + 6 * cost)
        # OUTPUT_HALT_EVENT_AFTER = A, the queue's pause threshold: a stall in the middle of a frame shorter than
        # A does not delay the frame's status word, one of A clocks does (queue and status rules), so A is the
        # largest stall without delay + 1, pinned by the stall of that length; the event output, which is high
        # for the stall's clocks beyond A, is a cross-check (a note)
        ideal_status = f["ideal"].d["status_hs"][0][0]
        status_delay = {s: x.d["status_hs"][0][0] - ideal_status for s, x in stall_cases.items()}
        on_time = [s for s, dl in status_delay.items() if dl == 0]
        after = max(on_time, default=-2) + 1
        self.evidence["OUTPUT_HALT_EVENT_AFTER"] = [[f"stall_{s}", dl] for s, dl in sorted(status_delay.items())]
        if after not in status_delay or status_delay[after] == 0 or any(
                status_delay[s] == 0 for s in status_delay if s > after):
            self.problems.append(f"OUTPUT_HALT_EVENT_AFTER: the status word delays of the stall_<s> cases do not "
                                 f"pin the shortest delaying stall (delay per s: {status_delay})")
            after = -1
        for s, x in sorted(stall_cases.items()):
            self.check(f"stall_{s}: event_data_out_channel_halt clocks",
                       len(x.d["events"]["event_data_out_channel_halt"]), max(s - after, 0), event=True)
        onsets = [(f"stall_{s}", x.d["events"]["event_data_out_channel_halt"][0] - x.d["out_stalled"][0])
                  for s, x in sorted(stall_cases.items()) if x.d["events"]["event_data_out_channel_halt"]]
        if onsets:
            self.agree("event_data_out_channel_halt onset (first event - first stalled clock)", onsets)

        # event_frame_started: measured on frames that start with the full slack; a frame that starts with less
        # (back to back after a free gap, or one idle clock after none) gives the pulse earlier, as its latency
        pulses = {c: x.d["events"]["event_frame_started"][:x.frames] for c, x in f.items()}
        starts = {c: x.start_slack(slack) for c, x in f.items()}
        started = self.agree("FRAME_STARTED_DELAY", [(f"{c} frame {k}", p - f[c].first_in(k)) for c in f
                                                     for k, p in enumerate(pulses[c]) if starts[c][k] == slack])
        for c in f:
            for k, p in enumerate(pulses[c]):
                if starts[c][k] != slack:
                    self.check(f"{c}: frame {k} event_frame_started after a start with slack {starts[c][k]}",
                               p - f[c].first_in(k), started - (slack - starts[c][k]), event=True)
        # Status word: measured without gaps and stalls (queue, status and pipeline input delay rules: below)
        status = self.agree("STATUS_AFTER_LAST_OUT", [(c, x.d["status_hs"][k][0] - x.last_out(k)) for c in nogap
                                                      for x in [f[c]] for k in range(x.frames)])
        config = []
        for c, x in f.items():
            valid, hs = x.d["cfg_valid"], x.d["cfg_hs"]
            for w in range(1, len(hs)):
                rise = min(v for v in valid if v > hs[w - 1])
                config.append((f"{c} word {w}", hs[w] - rise))
        config_accept = self.agree("CONFIG_ACCEPT_LOADING", config)
        b2b = f["back_to_back"]
        boundary = self.agree("FRAME_BOUNDARY_STALL", [("back_to_back", b2b.first_in(k) - b2b.last_in(k - 1) - 1)
                                                       for k in range(1, b2b.frames)])
        self.check("back_to_back: input stall clocks", len(b2b.d["in_refused"]), 0)

        # Halting: frame 1 of starve stops for a long gap; frame 0's output waits for the whole gap cost
        sv = f["starve"]
        gap = max(sv.gaps(1))
        gap_cost = xfft_timing.gap_cost([gap], slack)[0]
        delay = sv.lat_first(0) - lat
        halts = 1 if delay == gap_cost else 0 if delay == 0 else -1
        self.evidence["STARVE_HALTS_OUTPUT"] = [["starve", delay]]
        if halts < 0:
            self.problems.append(f"unexplained: starve: frame 0 delayed {delay} by a {gap}-clock gap of frame 1 "
                                 f"(neither 0 nor its cost {gap_cost})")
        h1 = f["halt_1"]
        hs2 = h1.d["in_hs"][2 * n:3 * n]
        before = [b - a - 1 for a, b in zip(hs2, hs2[1:]) if b - a > 1 and b <= h1.last_out(0)]
        self.check(f"halt_1: frame 0 last output with {len(before)} gaps of frame 2 before it", h1.lat_last(0),
                   lat_last + (xfft_timing.gap_cost(before, slack)[0] if halts == 1 else 0))

        # The cases that need a full input FIFO: unless the input was refused for longer than a frame boundary
        # before the output first moved after the stall that blocked it, their counts mean nothing (too few
        # frames for the latency)
        def releases(x: Facts) -> list[int]:
            """The first output handshake after each output stall."""
            stalled = set(x.d["out_stalled"])
            return [c for c in x.d["out_hs"] if c - 1 in stalled]

        def longest_refusal(x: Facts, before: int) -> int:
            runs, run, prev = [0], 0, None
            for c in x.d["in_refused"]:
                if c >= before:
                    break
                run = run + 1 if prev == c - 1 else 1
                runs.append(run)
                prev = c
            return max(runs)

        release_cases = {int(c[8:]): c for c in f if c.startswith("release_") and c[8:].isdigit()}
        dribble_cases = [c for c in f if c.startswith("dribble_")]
        refused = {c: longest_refusal(f[c], (releases(f[c]) or [f[c].d["out_hs"][0]])[0]) > boundary
                   for c in ("blocked", "fifo_backlog", *release_cases.values(), *dribble_cases) if c in f}
        for c, ok in refused.items():
            if not ok:
                self.problems.append(f"{c}: the input was not refused before the output moved; more frames needed")
        bl = f["blocked"]
        limit = sum(1 for c in bl.d["in_hs"] if c < bl.d["out_hs"][0]) if refused["blocked"] else -1
        self.evidence["INPUT_LIMIT"] = [["blocked", limit]]
        # Output buffer B: a stall of s >= OUTPUT_HALT_EVENT_AFTER clocks in the middle of a frame delays its
        # status word by s - B (the paused pipeline resumes with B samples queued); a shorter stall does not delay it
        buffer = self.agree("OUTPUT_BUFFER", [(f"stall_{s}", s - d) for s, d in sorted(status_delay.items())
                                              if s >= after])
        for s, d in sorted(status_delay.items()):
            if s < after:
                self.check(f"stall_{s}: status word delay (shorter than OUTPUT_HALT_EVENT_AFTER)", d, 0)
        if "stalls_1_5" in f:
            self.check("stalls_1_5: status word delay", f["stalls_1_5"].d["status_hs"][0][0] - ideal_status, 0)
        # ... and B samples stay queued: every later status word comes B clocks earlier against its last output
        for c in ("blocked", "fifo_backlog", *release_cases.values()):
            if refused.get(c):
                x = f[c]
                for k in range(x.frames):
                    self.check(f"{c}: frame {k} status word - last output (after a long stall)",
                               x.d["status_hs"][k][0] - x.last_out(k), status - buffer)

        # PIPELINE_LOOKAHEAD = K and OUTPUT_QUEUE_SIZE = Q, from the stall_end sweeps (cases.END_SWEEP): one stall
        # of s >= OUTPUT_HALT_EVENT_AFTER clocks before the frame's sample N - k, for every k of the sweep. In a
        # stream (stall_end_stream_<k>_<s>) the status word is on time up to k = A + K - 1, so the largest such k
        # less A - 1 measures K; at the end of the last frame (stall_end_<k>_<s>) up to k = Q + 1 while
        # A + K - 1 <= Q <= A + K (a paused pipeline puts out at most K more samples: a queue of A + K or more
        # never limits it, and one of A + K - 2 or less is not told apart from a stream), so the largest such k
        # less 1 measures Q. Each sweep must bracket its boundary: on time for every k up to it, delayed
        # for every k beyond (queue and status rules); every stall_end case's delay is checked below against
        # xfft_timing.status_delay_at().
        def sweep_cases(prefix: str) -> dict:
            return {c: (int(c.split("_")[-2]), int(c.split("_")[-1]), x) for c, x in f.items()
                    if c.startswith(prefix) and c.split("_")[-2].isdigit()}

        def word_delay(x: Facts) -> int:
            """Frame 0's status word, from its first input, against the ideal's."""
            return x.d["status_hs"][0][0] - x.first_in(0) - (ideal_status - f["ideal"].first_in(0))

        def sweep_edge(name: str, group: dict) -> int:
            """The largest k of a sweep whose status word is on time, when the sweep brackets it; else -1."""
            delays = {k: word_delay(x) for c, (k, s, x) in group.items() if after > 0 and s >= after}
            self.evidence[name] = [[c, word_delay(x)] for c, (k, s, x) in sorted(group.items(),
                                                                                 key=lambda i: i[1][0])]
            if after <= 0:
                # K and Q are read relative to A, so the sweep is read only within A's measurement window
                self.problems.append(f"{name}: OUTPUT_HALT_EVENT_AFTER was not pinned (stall_<s> cases), so the "
                                     f"sweep cannot be read: its boundary is read relative to it")
                return -1
            on_time = sorted(k for k, dl in delays.items() if dl == 0)
            edge = max(on_time, default=-1)
            if not on_time or edge == max(delays) or on_time != [k for k in sorted(delays) if k <= edge]:
                self.problems.append(f"{name}: the sweep of k = {min(delays, default='-')} .. "
                                     f"{max(delays, default='-')} does not bracket the boundary between the status "
                                     f"words on time and delayed (on time: {on_time}; widen cases.END_SWEEP)")
                return -1
            return edge

        ends = {c: v for c, v in sweep_cases("stall_end_").items() if not c.startswith("stall_end_stream_")}
        streams = sweep_cases("stall_end_stream_")
        # The k positions presuppose no constant, but K and Q are read relative to A: they inherit A's measurement
        # window from the stall_<s> set (A pinnable only at 2, 14 or 15)
        stream_edge = sweep_edge("PIPELINE_LOOKAHEAD", streams)
        look = stream_edge - (after - 1) if stream_edge > 0 else -1
        if stream_edge > 0 and look < 1:
            self.problems.append(f"PIPELINE_LOOKAHEAD: the largest k on time in a stream ({stream_edge}) less "
                                 f"OUTPUT_HALT_EVENT_AFTER - 1 is {look}, not a lookahead of 1 or more")
            look = -1
        if look > 0:
            self.evidence["PIPELINE_LOOKAHEAD"].insert(0, [f"largest k on time in a stream ({stream_edge}) less "
                                                           f"OUTPUT_HALT_EVENT_AFTER - 1", look])
        end_edge = sweep_edge("OUTPUT_QUEUE_SIZE", ends)
        size = -1
        if end_edge > 0 and look > 0:
            size = min(end_edge - 1, after + look)
            if end_edge < stream_edge + 1:
                self.problems.append(f"OUTPUT_QUEUE_SIZE: at the end of the last frame the status word is on time "
                                     f"only up to k = {end_edge}, as in a stream ({stream_edge}): a queue of "
                                     f"OUTPUT_HALT_EVENT_AFTER + PIPELINE_LOOKAHEAD - 2 or less, not pinned")
                size = -1
            elif end_edge - 1 > after + look:
                self.problems.append(f"OUTPUT_QUEUE_SIZE: on time at the end of the last frame up to k = {end_edge}, "
                                     f"beyond OUTPUT_HALT_EVENT_AFTER + PIPELINE_LOOKAHEAD + 1: not the queue rule")
                size = -1
            else:
                if end_edge - 1 == after + look:
                    self.observations.append("OUTPUT_QUEUE_SIZE = OUTPUT_HALT_EVENT_AFTER + PIPELINE_LOOKAHEAD: the "
                                             "queue never limits the pipeline, any larger size is the same")
                self.evidence["OUTPUT_QUEUE_SIZE"].insert(0, [f"largest k on time at the end of the last frame "
                                                              f"({end_edge}) less 1", size])
        timing = {"OUTPUT_HALT_EVENT_AFTER": after, "OUTPUT_BUFFER": buffer, "STATUS_AFTER_LAST_OUT": status,
                  "PIPELINE_LOOKAHEAD": look, "OUTPUT_QUEUE_SIZE": size}
        queue_known = look > 0 and size > 0

        # Pipeline input delay D = FRAME_STARTED_DELAY - INPUT_SLACK: a gap that costs (slack s left before it,
        # first gap clock g) halts the output from clock g + s + 1 + D, when the output streams then
        measured_d = []
        for c in ("halt_1", "halt_dense", "halt_boundary"):
            if c not in f:
                continue
            x = f[c]
            fr = x.frames - 1
            hs = x.d["in_hs"][fr * n:(fr + 1) * n]
            starts = [a + 1 for a, b in zip(x.d["out_hs"], x.d["out_hs"][1:]) if b - a > 1]
            left = x.start_slack(slack)[fr]
            found = False
            for a, b in zip(hs, hs[1:]):
                if b - a > 1:
                    gap_delay, after_left = xfft_timing.gap_cost([b - a - 1], slack, left)
                    if gap_delay and a + 1 > x.d["out_hs"][0]:
                        first = min((m for m in starts if m > a), default=None)
                        if first is not None:
                            measured_d.append((f"{c} gap at {a + 1}", first - (a + 1) - left - 1))
                            found = True
                        break
                    left = after_left
            if not found:
                self.problems.append(f"{c}: no costing gap halts a streaming output, so no evidence for the "
                                     f"pipeline input delay (its positions do not fit N = {n})")
        d = self.agree("pipeline input delay (derived: FRAME_STARTED_DELAY - INPUT_SLACK)", measured_d)
        self.check("pipeline input delay = FRAME_STARTED_DELAY - INPUT_SLACK", d, started - slack)
        d = started - slack

        # The output and status channels while the pipeline starves, in every variation without output stalls:
        # the output register is low exactly in the clocks the gaps' costs halt the pipeline (inside a frame's
        # output), and a frame's status word follows the status rule (xfft_timing.status_word_clock(), from the
        # clock the pipeline put out the frame's sample N - 1 - K, the pipeline running in every clock in which
        # it does not starve). halt_boundary puts such halts on the boundary between two frames' output, halt_tail
        # one after every output sample (so also right after sample N - 1 - K, where the word waits longest).
        boundary_seen, tail_seen = False, False
        for c, x in f.items():
            if x.d["out_stalled"] or not queue_known:
                continue
            starve_clocks = self.starving(x, slack, d)
            low, predicted = self.output_low(x, starve_clocks)
            self.check(f"{c}: clocks of output tvalid low inside the frames, not those of a starving pipeline",
                       len(low ^ predicted), 0)
            for k in range(x.frames):
                put = x.d["out_hs"][k * n + n - 1 - look] - 1
                while put in starve_clocks:
                    put -= 1
                due = xfft_timing.status_word_clock(put, starve_clocks.__contains__,
                                                    lambda t: t not in starve_clocks, timing)
                boundary_seen |= c == "halt_boundary" and due != x.last_out(k) + status
                tail_seen |= c == "halt_tail" and put + 1 in starve_clocks
                self.check(f"{c}: frame {k} status word (waits while the pipeline starves)",
                           x.d["status_hs"][k][0], due)
        if "halt_boundary" in f and not boundary_seen:
            self.notes.append("halt_boundary: no starving halt fell on a frame boundary (its positions fit "
                              "LATENCY_FIRST_OUT - 2N of about 110 to 135; the other checks of the variation passed)")
        if "halt_tail" in f and not tail_seen:
            self.problems.append(f"halt_tail: no frame's pipeline starved right after it put out sample N - 1 - K, "
                                 f"so no evidence for the status rule's first step (its positions do not fit "
                                 f"N = {n})")
        # The queue under starving (queue rule): after an output stall of s clocks, s samples are queued when
        # s < OUTPUT_HALT_EVENT_AFTER, else B, and the input FIFO holds max(0, s - B) samples more; the loading
        # frame's gap clocks use the FIFO's up first, the starving clocks of its costing gaps then drain the
        # queue, and only the rest halt the output
        for c in ("stall_then_halt", "short_stall_then_halt"):
            if c not in f:
                continue
            x = f[c]
            s = sum(sum(v.values()) for v in x.d["stalls"].values())
            first_stall, stall_end = x.d["out_stalled"][0], x.d["out_stalled"][-1]
            backlog = s - buffer if s >= after else 0
            starve_clocks = sorted(t for t in self.starving(x, slack, d, first_stall, backlog) if t > stall_end)
            if not any(t > stall_end for t in self.starving(x, slack, d)):
                self.problems.append(f"{c}: no costing gap after the stall, so no evidence for the FIFO backlog "
                                     f"and the queue (its positions do not fit N = {n})")
                continue
            low, _ = self.output_low(x, set())
            low = {t for t in low if t > stall_end}
            queued = buffer if s >= after else s
            self.check(f"{c}: output halt clocks after a {s}-clock stall that are not the starving clocks beyond "
                       f"the {queued} queued samples ({len(starve_clocks)} starving)",
                       len(low ^ set(starve_clocks[queued:])), 0)

        # Input resume: after a blocked output the pipeline restarts A - B clocks after the first output handshake
        # of a release (queue rule) and takes one sample per clock; the input side sees each sample that leaves
        # the FIFO D - K clocks late, so the input resumes INPUT_RESUME + (A - B) + (D - K) clocks after the
        # release's first output, and takes as many samples as left. release_<k>: a release of k samples into a
        # full FIFO; the shortest one that resumes the input within it gives INPUT_RESUME, a release of one sample
        # less (that does not) pins it.
        def resumed(x: Facts, after_clock: int) -> int:
            """The first input handshake after the input was refused, after the given clock."""
            refused = [c for c in x.d["in_refused"] if c >= after_clock]
            return min(c for c in x.d["in_hs"] if c > refused[0])

        within = {}
        for k, c in sorted(release_cases.items()):
            if refused.get(c):
                x = f[c]
                r1, r2 = releases(x)[:2]
                self.check(f"{c}: samples out in the first release",
                           sum(1 for t in x.d["out_hs"] if r1 <= t < r2), k)
                within[k] = resumed(x, 0) < r2
        self.evidence["INPUT_RESUME"] = [[release_cases[k], int(w)] for k, w in sorted(within.items())]
        resuming = [k for k, w in within.items() if w]
        resume = min(resuming) if resuming else -1
        if resume < 0 or within.get(resume - 1, True):
            self.problems.append(f"INPUT_RESUME: no release_<k> pair at adjacent k pins the shortest release that "
                                 f"resumes the input (resumed within the release: {within})")
            resume = -1
        lag = []
        for c in ("blocked", "fifo_backlog", *[release_cases[k] for k in sorted(within) if within[k]]):
            if refused.get(c) and resume > 0:
                x = f[c]
                lag.append((c, resumed(x, 0) - releases(x)[0] - resume))
        restart = (after - buffer) + (d - look)      # the pipeline's restart and the input side's lag
        if lag and queue_known:
            lag = self.agree("input resume delay (the restart A - B + the input side's lag D - K)", lag)
            self.check("input resume delay = (OUTPUT_HALT_EVENT_AFTER - OUTPUT_BUFFER) + (FRAME_STARTED_DELAY - "
                       "INPUT_SLACK - PIPELINE_LOOKAHEAD)", lag, restart)
        for k, c in sorted(release_cases.items()):
            if not refused.get(c) or resume < 0 or not queue_known:
                continue
            x = f[c]
            r1, r2 = releases(x)[:2]
            first = resumed(x, 0)
            if within[k]:
                again = min(t for t in x.d["in_refused"] if t > first)
                self.check(f"{c}: samples accepted until the FIFO is full again = samples out in the release",
                           sum(1 for t in x.d["in_hs"] if first <= t < again), k)
            else:
                self.check(f"{c}: input resume after the next release ({k} samples out in the first)",
                           first - r2, resume - k + restart)

        # The queue rule, clock by clock, in the dribble_<k..> cases (a blocked output released in dribbles):
        # from the first release on the pipeline runs while fewer than OUTPUT_HALT_EVENT_AFTER samples wait,
        # once paused until only B wait; the input takes a sample in every clock in which the input side's count
        # (the samples that left seen D - K clocks late) is below the FIFO's size, from INPUT_RESUME below it on
        for c in dribble_cases:
            x = f[c]
            if not refused.get(c) or resume < 0 or not queue_known:
                continue
            rel = releases(x)
            end = rel[-1] + 2 * resume + d
            predicted = self.dribble_inputs(x.d["out_hs"], rel[0], end, after, buffer, resume, d - look)
            actual = [t for t in x.d["in_hs"] if rel[0] <= t < end]
            first = next((i for i, (a, b) in enumerate(zip(actual, predicted)) if a != b),
                         min(len(actual), len(predicted)))
            self.check(f"{c}: input handshakes from the first release to {end - rel[-1]} clocks into the last that "
                       f"the queue and input resume rules predict ({len(predicted)}; the first other one: "
                       f"{actual[first:first + 1]} against {predicted[first:first + 1]})",
                       sum(1 for a, b in zip(actual, predicted) if a == b), max(len(actual), len(predicted)))
        if not dribble_cases:
            self.problems.append("dribble_*: no dribble case, so no evidence for the pipeline's restart after A - B "
                                 "(OUTPUT_HALT_EVENT_AFTER - OUTPUT_BUFFER)")

        for c, (k, s, x) in sorted({**ends, **streams}.items()) if queue_known else ():
            followed = c in streams
            self.check(f"{c}: status word delay (a {s}-clock stall before the {k}-th last sample, "
                       f"{'in a stream' if followed else 'the last frame'})", word_delay(x),
                       xfft_timing.status_delay_at(k, s, timing, followed))
        # stall_end_idle*: a long stall among frame 0's last samples (the case's own, from cases.py), idle clocks
        # before frame 1: the pipeline runs idle - (the slack frame 0 left - the slack frame 1 starts with) clocks
        # without a sample between the frames, and the frame's last samples that fit into the queue come out during the
        # stall when one of those clocks is among the next K + 1 (a stream after 1 idle clock with the slack
        # full, as at the end of the last frame after 2 or more)
        idles = [c for c in f if c.startswith("stall_end_idle")]
        if not idles:
            self.problems.append("stall_end_idle*: no case, so no evidence for the queue rule before idle clocks")
        defs = cases.cases(n)
        for c in idles if queue_known else ():
            x = f[c]
            (pos, length), = defs[c].stalls[0].items()
            idle = x.idle(1)
            left = xfft_timing.frame_gap_cost(x.gap_map(0), n, slack)[1]
            gap = idle - (left - x.start_slack(slack)[1])
            self.check(f"{c}: frame 0 status word delay (a {length}-clock stall before the {n - pos}-th last "
                       f"sample, {idle} idle clocks after the frame: {gap} pipeline clocks without a sample)",
                       word_delay(x), xfft_timing.status_delay_at(n - pos, length, timing, True, gap))
        return {"LATENCY_FIRST_OUT": lat, "LATENCY_LAST_OUT": lat_last, "INPUT_SLACK": slack,
                "OUTPUT_STALL_COST": cost, "RESET_TO_READY": reset, "CONFIG_ACCEPT_LOADING": config_accept,
                "FRAME_BOUNDARY_STALL": boundary, "STARVE_HALTS_OUTPUT": max(halts, 0),
                "OUTPUT_HALT_EVENT_AFTER": after, "FRAME_STARTED_DELAY": started,
                "STATUS_AFTER_LAST_OUT": status, "INPUT_LIMIT": limit, "OUTPUT_BUFFER": buffer,
                "INPUT_RESUME": resume, "PIPELINE_LOOKAHEAD": look, "OUTPUT_QUEUE_SIZE": size}

    @staticmethod
    def starving(x: Facts, slack: int, d: int, after_clock: int = -1, backlog: int = 0) -> set[int]:
        """The clocks in which the pipeline starves (pipeline input delay rule): each gap that costs, with slack
        s left before it and first gap clock g, from g + s + 1 + D for its cost. A FIFO backlog of `backlog`
        samples (after an output stall that halted the pipeline) is used up first by the gap clocks after
        `after_clock`."""
        out = set()
        n = x.n
        for fr, (left, restart) in enumerate(x.starts(slack)):
            hs = x.d["in_hs"][fr * n:(fr + 1) * n]
            kind, restarted = restart, 0 if restart else -1     # the last sample that restarted the pipeline
            for k, (a, b) in enumerate(zip(hs, hs[1:]), 1):
                length = b - a - 1
                if length <= 0:
                    continue
                if a + 1 > after_clock and backlog > 0:
                    used = min(backlog, length)
                    backlog -= used
                    length -= used
                    if length == 0:
                        continue
                cost, after_left, after = xfft_timing.gap_step(length, slack, left,
                                                               kind if restarted == k - 1 else 0)
                start = a + 1 + left + 1 + d
                out.update(range(start, start + cost))
                left = after_left
                if after:
                    kind, restarted = after, k
        return out

    @staticmethod
    def dribble_inputs(outs: list[int], first: int, end: int, after: int, buffer: int, resume: int,
                       lag: int) -> list[int]:
        """The input handshakes in [first, end) after a blocked output (pipeline paused with `after` samples
        waiting, input FIFO full), given the output handshakes and an input offered in every clock (the queue
        and input resume rules)."""
        out = set(outs)
        wait, paused, left, taken, full = after, True, [], [], True
        for t in range(first, end):
            paused = wait >= after or (paused and wait > buffer)
            if t in out:
                wait -= 1
            if not paused:
                wait += 1
                left.append(t)
            if not full:
                taken.append(t)
            count = len(taken) - sum(1 for u in left if u <= t - lag)
            full = count >= 0 or (full and count > -resume)
        return taken

    @staticmethod
    def output_low(x: Facts, halts: set[int]) -> tuple[set[int], set[int]]:
        """(clocks inside a frame's output with m_axis_data_tvalid low, the given halt clocks among them)."""
        n = x.n
        inside = set()
        for k in range(x.frames):
            inside.update(range(x.first_out(k), x.last_out(k)))
        offered = set(x.d["out_hs"]) | set(x.d["out_stalled"])
        return {t for t in inside if t not in offered}, halts & inside


def cache_keys(tag: str, names) -> dict[str, str]:
    """The cache key of each rtl run, from the store's index (the entry the latest run hit or stored)."""
    try:
        store = sim_cache.open_store()
    except sim_cache.CacheError:
        return {}
    keys = {}
    for c in names:
        data = store.get(sim_cache.index_name(f"test_characterize.{tag}/{c}_rtl"))
        if data:
            keys[c] = json.loads(data)["key"]
    return keys


def main() -> int:
    model = sys.argv[2] if sys.argv[1:2] == ["--from"] else "rtl"
    model = "engine" if model == "cmodel" else model        # the stand-in's earlier name
    if model not in cases.MODELS:
        print(f"extract: --from {model}: one of {', '.join(cases.MODELS)}", file=sys.stderr)
        return 1
    cfg = xfft_config.current()
    names = [c for c, case in cases.cases(cfg.points).items() if not case.known]
    missing = [c for c in names if not cases.json_path(cfg.tag, c, model).is_file()]
    if missing:
        print(f"extract: no {model} facts for {', '.join(missing)}: run python -m characterize --models {model} "
              f"first",
              file=sys.stderr)
        return 1
    facts = {c: Facts(json.loads(cases.json_path(cfg.tag, c, model).read_text())) for c in names}
    ex = Extraction(facts)
    constants = ex.run()
    relations = {}
    if all(v >= 0 for v in constants.values()):
        for name, sides in xfft_timing.RELATIONS.items():
            left, right = relations[name] = list(sides(constants))
            ex.observations.append(f"{name}: {'holds' if left == right else 'DOES NOT hold'} ({left} against "
                                   f"{right})")
    versions = sorted({x.d["core_version"] for x in facts.values()})
    doc = {"tag": cfg.tag, "config": dataclasses.asdict(cfg), "core_version": versions[0] if len(versions) == 1
           else versions, "constants": constants, "evidence": ex.evidence, "relations": relations,
           "provenance": {"extractor_version": EXTRACTOR_VERSION, "rtl_runs": cache_keys(cfg.tag, names)}}
    if ex.problems:
        doc["problems"] = ex.problems
    out = cases.OUT / f"{cfg.tag}.json"
    missing = sorted(k for k, v in constants.items() if v < 0)
    if model == "rtl" and missing:
        # No stand-in may be built from constants without evidence: a stale file goes too
        out.unlink(missing_ok=True)
        print(f"extract: no evidence for {', '.join(missing)}: {out.relative_to(REPO)} not written")
    elif model == "rtl":
        cases.OUT.mkdir(exist_ok=True)
        out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")

    print(f"extract: {cfg.tag} ({model} facts), xfft {doc['core_version']}, {len(names)} variations, "
          f"{len(ex.explained)} checks explained by the constants")
    for p in ex.problems:
        print(f"extract: PROBLEM {p}")
    for p in ex.notes:
        print(f"extract: note (an event output, not in the timing scope): {p}")
    for p in ex.observations:
        print(f"extract: observation (not assumed, fails nothing): {p}")
    print(f"\nTABLE entry ({out.relative_to(REPO)}):\n    \"{cfg.tag}\": {{")
    for k, v in constants.items():
        print(f"        \"{k}\": {v},")
    print("    },")
    table = xfft_timing.TABLE.get(cfg.tag)
    if table is None:
        print("extract: TABLE in model/xfft_timing.py has no entry for this configuration")
    else:
        diff = {k: (table.get(k), v) for k, v in constants.items() if table.get(k) != v}
        print("extract: equal to TABLE in model/xfft_timing.py" if not diff else f"extract: differs from TABLE: {diff}")
    return 1 if ex.problems else 0


if __name__ == "__main__":
    sys.exit(main())
