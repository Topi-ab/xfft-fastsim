"""Timing bench of the xfft core in plain XSim (tb_xfft_timing.sv): measures the timing constants of
model/xfft_timing.py on the real core, checks them, and measures simulation speed. Each case runs as its own
XSim process in its own run directory, so all cases can run at the same time.

    python tests/timing/xfft_timing_bench.py validate            check the constants, all cases in parallel
    python tests/timing/xfft_timing_bench.py validate -j 2       at most 2 processes at a time
    python tests/timing/xfft_timing_bench.py validate gap_2 starve      some cases only
    python tests/timing/xfft_timing_bench.py measure             derive the constants of the current
                                                                 configuration (XFFT_CONFIG) from the real core
    python tests/timing/xfft_timing_bench.py speed --frames 20   simulated clocks per second
    python tests/timing/xfft_timing_bench.py probe g256:1,g512:5 g512:1   one run per argument with input
                                     gaps (g<sample>:<clocks>) and output stalls (s<output>:<clocks>), up to 4
                                     of each, positions over all frames (--frames, default 1); prints the
                                     latencies of the first 3 frames, for exploring a core's timing

--model (default: FFT_MODEL, else rtl) picks the module under test: rtl, the generated core, or engine, the
stand-in (the C engine through model/xfft_engine.sv; validate on engine checks that the stand-in follows its own
constants). measure always uses rtl.

`measure` and `validate` are kept apart: measure derives each constant from a few cases and writes them to
build/<configuration>/timing_measured.json (used by model/xfft_timing.py for configurations not in its TABLE)
and prints the TABLE entry; validate runs every case, also the ones no constant was derived from, and checks
each prediction of the constants in effect (model/xfft_timing.constants()).

Builds into build/<configuration>/timing/: the C engine's library and package in dpi_engine/
(scripts/build_engine.py; every model links the library for its wall clock, the package has the widths and
constants), the core's simulation files (rtl, through cocotb-vivado's VivadoIp), the snapshot of each model and
runs/<model>/<case>/ per run. Needs the Vivado tools on PATH (settings64.sh) and a C compiler (gcc, or CC).
Exit code 0 when every check of every case passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO / "model"), str(REPO / "scripts")]
import build_engine  # noqa: E402
import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402

SNAPSHOT = "timing"
LIBRARY = "xil_defaultlib"
MODELS = ("rtl", "engine")
# name -> plusargs of tb_xfft_timing.sv
def probe(frames: int = 1, **gaps: int) -> dict:
    """Plusargs of a probe run: gaps as at<position>=<clocks> (position over all frames)."""
    plus = {"CASE": "probe", "FRAMES": frames}
    for i, (pos, length) in enumerate(gaps.items()):
        plus[f"GAPPOS{i}"], plus[f"GAPLEN{i}"] = int(pos[2:]), length
    return plus


def all_cases(n: int) -> dict:
    """name -> plusargs of tb_xfft_timing.sv, for N = n points."""
    q, h = n // 4, n // 2
    return CASES | {
        # Input slack: several gaps in one frame, idle clocks between frames
        "gaps_1_1": probe(**{f"at{q}": 1, f"at{h}": 1}),
        "gaps_1_5": probe(**{f"at{q}": 1, f"at{h}": 5}),
        "gaps_5_1": probe(**{f"at{q}": 5, f"at{h}": 1}),
        "gaps_1_1_1": probe(**{f"at{q}": 1, f"at{h}": 1, f"at{h + q}": 1}),
        "idle_1": probe(2, **{f"at{n}": 1}),
        "idle_5": probe(2, **{f"at{n}": 5}),
        "gap_1_then_back_to_back": probe(2, **{f"at{h}": 1}),
        "gap_1_then_idle_1": probe(2, **{f"at{h}": 1, f"at{n}": 1}),
    }


# Gaps (position in frame / N, clocks) of the probe cases, and idle clocks before frame 1
PROBE_GAPS = {"gaps_1_1": [1, 1], "gaps_1_5": [1, 5], "gaps_5_1": [5, 1], "gaps_1_1_1": [1, 1, 1],
              "idle_1": [], "idle_5": [], "gap_1_then_back_to_back": [1], "gap_1_then_idle_1": [1]}
PROBE_IDLE = {"idle_1": 1, "idle_5": 5, "gap_1_then_back_to_back": 0, "gap_1_then_idle_1": 1}

RELEASES = range(1, 9)
# Output stalls in the middle of the frame around the queue's pause threshold OUTPUT_HALT_EVENT_AFTER: the
# shortest that delays the status word is it
STALL_SWEEP = range(8, 21)
# A long output stall before sample N - k of frame 0, for every k, in a stream (stall_end_stream_<k>: the next frame
# behind it) and at the end of the last frame (stall_end_<k>): the largest k whose status word is on time gives
# PIPELINE_LOOKAHEAD and OUTPUT_QUEUE_SIZE (the characterization bench's stall_end sweeps, cases.py)
END_SWEEP = range(1, 41)
END_STALL = 100
CASES = {
    "first_out": {"CASE": "first_out"},
    "last_out": {"CASE": "last_out"},
    "gap_1": {"CASE": "gap", "LEN": 1},
    "gap_2": {"CASE": "gap", "LEN": 2},
    "gap_3": {"CASE": "gap", "LEN": 3},
    "gap_100": {"CASE": "gap", "LEN": 100},
    "stall_1": {"CASE": "stall", "LEN": 1},
    "stall_100": {"CASE": "stall", "LEN": 100},
    **{f"stall_{k}": {"CASE": "stall", "LEN": k} for k in STALL_SWEEP},
    "stall_end": {"CASE": "stall_end", "LEN": 30, "FROM_END": 18},
    **{f"stall_end_{k}": {"CASE": "stall_end", "LEN": END_STALL, "FROM_END": k} for k in END_SWEEP},
    **{f"stall_end_stream_{k}": {"CASE": "stall_end", "LEN": END_STALL, "FROM_END": k, "FRAMES": 2}
       for k in END_SWEEP},
    "back_to_back": {"CASE": "back_to_back"},
    "starve": {"CASE": "starve"},
    "halt_1": {"CASE": "halt_1"},
    "blocked": {"CASE": "blocked"},
    "resume": {"CASE": "resume"},
    # A release of k output samples into a full input FIFO: the shortest that lets the input in is INPUT_RESUME
    **{f"release_{k}": {"CASE": "release", "LEN": k} for k in RELEASES},
}
# The cases measure derives the constants from; validate runs all of CASES
MEASURE_CASES = ("first_out", "last_out", "gap_1", "gap_2", "gap_3", "stall_100", "stall_end", "back_to_back",
                 "starve", "blocked", "resume", *(f"release_{k}" for k in RELEASES),
                 *(f"stall_{k}" for k in STALL_SWEEP), *(f"stall_end_{k}" for k in END_SWEEP),
                 *(f"stall_end_stream_{k}" for k in END_SWEEP))


def log(msg: str) -> None:
    print(f"xfft_timing_bench: {msg}", flush=True)


def tool(cmd: list, cwd: Path, log_name: str) -> None:
    with open(cwd / log_name, "w") as f:
        result = subprocess.run([str(c) for c in cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        text = (cwd / log_name).read_text(errors="replace").splitlines()
        errors = [line for line in text if "ERROR" in line] or text[-20:]
        sys.exit("\n".join([f"xfft_timing_bench: {cmd[0]} failed, see {cwd / log_name}", *errors]))


def sha256(paths: list[Path], *texts: str) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    for t in texts:
        h.update(t.encode())
    return h.hexdigest()


def build(cfg: xfft_config.XfftConfig, model: str) -> Path:
    """Compile and elaborate tb_xfft_timing with the model; the elaboration directory."""
    root = cfg.build_dir / "timing"
    dpi = root / "dpi_engine"
    elab = root / model
    elab.mkdir(parents=True, exist_ok=True)
    build_engine.build(dpi)
    pkg = dpi / "xfft_model_pkg.sv"         # its first line names the library's digest
    if model != "rtl" and "XFFT_HAS_TIMING = 0" in pkg.read_text():
        xfft_timing.constants(cfg)          # raises with the instructions
    tb = HERE / "tb_xfft_timing.sv"
    builder = cfg.write_builder_tcl()
    model_sv = REPO / "model" / "xfft_engine.sv"
    stamp = elab / "inputs.sha256"
    digest = sha256([pkg, tb, Path(__file__), *([model_sv] if model != "rtl" else [builder])], model)
    if stamp.exists() and stamp.read_text() == digest and (elab / "xsim.dir" / SNAPSHOT / "xsimk").exists():
        return elab
    stamp.unlink(missing_ok=True)
    defines = ["-d", f"XFFT_CORE_NAME={cfg.module}"]
    libs, tops = [], []
    if model == "rtl":
        from cocotb_vivado.vivado import VivadoIp

        log(f"generating the core {cfg.tag} (Vivado)")
        info = VivadoIp(cfg.xci, builder_tcl=builder, part_num=cfg.part).prepare(elab, LIBRARY)
        for prj, compiler in ((info.vhdl_prj, "xvhdl"), (info.vlog_prj, "xvlog")):
            if prj is not None:
                tool([compiler, "--relax", "-work", LIBRARY, "-prj", prj], elab, f"{compiler}_ip.log")
        libs, tops = sorted(info.libraries), list(info.glbl_modules)
        sv = [pkg, tb]
    else:
        sv = [pkg, model_sv, tb]
    log(f"compiling and elaborating the bench on {model}")
    tool(["xvlog", "-sv", "-work", LIBRARY, *defines, *sv], elab, "xvlog.log")
    # -sv_root other than the elaboration directory: the snapshot links the library by its absolute path, so it
    # runs from any directory (with the elaboration directory as -sv_root it would link ./libxfft_engine.so)
    dpi_libs = ["-sv_root", dpi, "-sv_lib", "libxfft_engine"]
    tool(["xelab", *[a for lib in libs for a in ("-L", lib)], "-L", LIBRARY, *dpi_libs, "-timescale", "1ps/1ps",
          "-snapshot", SNAPSHOT, f"{LIBRARY}.tb_xfft_timing", *tops], elab, "xelab.log")
    stamp.write_text(digest)
    return elab


def run_case(elab: Path, run_dir: Path, case: str, plusargs: dict) -> dict:
    """One XSim run in its own directory, with its own copy of the snapshot."""
    shutil.rmtree(run_dir, ignore_errors=True)
    (run_dir / "xsim.dir").mkdir(parents=True)
    shutil.copytree(elab / "xsim.dir" / SNAPSHOT, run_dir / "xsim.dir" / SNAPSHOT)
    cmd = ["xsim", SNAPSHOT, "-R", "-log", "xsim.log"]
    for k, v in plusargs.items():
        cmd += ["-testplusarg", f"{k}={v}"]
    t0 = time.monotonic()
    with open(run_dir / "xsim.out", "w") as out:
        proc = subprocess.Popen(cmd, cwd=run_dir, stdout=out, stderr=subprocess.STDOUT)
        _, status, usage = os.wait4(proc.pid, 0)    # CPU time of xsim and the xsimk it waits for
    wall = time.monotonic() - t0
    text = (run_dir / "xsim.out").read_text(errors="replace")
    values = {}
    for line in text.splitlines():
        if line.startswith("XFFT_TIMING_RESULT "):
            values = {k: float(v) if "." in v else int(v) for k, v in re.findall(r"(\w+)=(-?[\d.]+)", line)}
    errors = [line for line in text.splitlines() if "Fatal" in line or "FATAL" in line or "ERROR" in line]
    return {"case": case, "wall": wall, "cpu": usage.ru_utime + usage.ru_stime,
            "rc": os.waitstatus_to_exitcode(status), "values": values, "errors": errors}


def run_cases(cfg: xfft_config.XfftConfig, model: str, cases: dict, jobs: int) -> dict:
    elab = build(cfg, model)
    runs = cfg.build_dir / "timing" / "runs" / model
    log(f"running {len(cases)} case(s) on {model}, {jobs or len(cases)} at a time")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs or len(cases)) as pool:
        results = list(pool.map(lambda c: run_case(elab, runs / c, c, cases[c]), cases))
    wall = time.monotonic() - t0
    for r in results:
        clocks = r["values"].get("clocks", 0)
        log(f"{r['case']}: {r['wall']:.0f} s wall, {r['cpu']:.0f} s CPU, {clocks} clocks, xsim exit {r['rc']}"
            + "".join(f"\n    {e}" for e in r["errors"]))
        if r["rc"] != 0 or not r["values"]:
            sys.exit(f"xfft_timing_bench: {r['case']} did not finish, see {runs / r['case'] / 'xsim.log'}")
    case_wall = sum(r['wall'] for r in results)
    log(f"{len(results)} case(s) in {wall:.0f} s wall (sum of the case wall times {case_wall:.0f} s, "
        f"CPU {sum(r['cpu'] for r in results):.0f} s)")
    return {r["case"]: r["values"] for r in results}


# ---- Constants from measurements, and their checks ----

def derive(m: dict) -> dict:
    """The timing constants from the MEASURE_CASES results (raises when a rule does not hold)."""
    lat = m["first_out"]["first_out"]
    lat_last = m["last_out"]["last_out"]
    deltas = [m[f"gap_{n}"]["first_out"] - lat for n in (1, 2, 3)]
    free = 0
    while free < 3 and deltas[free] == 0:
        free += 1
    if any(d != n for n, d in zip((1, 2, 3), deltas) if n > free):
        raise ValueError(f"input gaps of 1, 2, 3 clocks delay the frame by {deltas}: not 'free up to the slack "
                         f"S, else the whole gap'")
    stall = m["stall_100"]
    if (stall["last_out"] - lat_last) % 100:
        raise ValueError(f"a 100-clock output stall costs {stall['last_out'] - lat_last} clocks")
    starve = m["starve"]
    if starve["first_out"] - lat not in (0, starve["max_gap"]):
        raise ValueError(f"starving frame 1 for {starve['max_gap']} clocks delays frame 0 by "
                         f"{starve['first_out'] - lat}: neither 0 nor the whole gap")
    b2b = m["back_to_back"]
    blocked = m["blocked"]
    first = m["first_out"]
    if first["reset_to_cfg_ready"] != first["reset_to_data_ready"]:
        raise ValueError(f"config and data tready rise apart after reset: {first['reset_to_cfg_ready']} / "
                         f"{first['reset_to_data_ready']}")
    status = m["last_out"]["status"]
    # The queue's pause threshold A = OUTPUT_HALT_EVENT_AFTER: a stall of s clocks in the middle of the frame
    # delays the status word (by s - OUTPUT_BUFFER) from s = A on; event_data_out_channel_halt (high for the
    # stall's clocks beyond A) is only checked by validate
    delays = {k: m[f"stall_{k}"]["status"] - status + k for k in STALL_SWEEP}
    halt_after = max((k for k, v in delays.items() if v == 0), default=min(STALL_SWEEP) - 1) + 1
    if halt_after not in delays or any((v == 0) != (k < halt_after) for k, v in delays.items()):
        raise ValueError(f"output stalls of {min(STALL_SWEEP)} .. {max(STALL_SWEEP)} clocks delay the status word "
                         f"by {delays}: not 'none below OUTPUT_HALT_EVENT_AFTER, else s - OUTPUT_BUFFER' within "
                         f"the sweep (extend STALL_SWEEP)")
    buffer = status - stall["status"]
    # PIPELINE_LOOKAHEAD = K and OUTPUT_QUEUE_SIZE = Q from the stall_end sweeps (the queue and status rules of
    # model/xfft_timing.py): a long stall before sample N - k of frame 0 leaves its status word on time up to
    # k = A + K - 1 in a stream and up to k = Q + 1 at the end of the last frame (for A + K - 1 <= Q <= A + K:
    # a larger queue never limits the pipeline, the same as A + K; a smaller one is not told apart from a
    # stream), delayed beyond
    ideal = status + lat_last

    def edge(prefix: str) -> int:
        on_time = {k: m[f"{prefix}{k}"]["status"] + m[f"{prefix}{k}"]["last_out"] == ideal for k in END_SWEEP}
        largest = max((k for k, v in on_time.items() if v), default=None)
        if largest is None or largest == max(END_SWEEP) or any(v != (k <= largest) for k, v in on_time.items()):
            raise ValueError(f"{prefix}<k>: a {END_STALL}-clock stall before sample N - k leaves the status word on "
                             f"time for k = {sorted(k for k, v in on_time.items() if v)} of {min(END_SWEEP)} .. "
                             f"{max(END_SWEEP)}: no boundary bracketed (extend END_SWEEP)")
        return largest

    stream_edge, end_edge = edge("stall_end_stream_"), edge("stall_end_")
    look = stream_edge - (halt_after - 1)
    if look < 1 or not stream_edge + 1 <= end_edge <= halt_after + look + 1:
        raise ValueError(f"the status word is on time up to k = {stream_edge} in a stream and {end_edge} at the end "
                         f"of the last frame: not a lookahead K >= 1 and a queue size Q of A + K - 1 .. A + K")
    size = end_edge - 1
    rules = {"OUTPUT_HALT_EVENT_AFTER": halt_after, "OUTPUT_BUFFER": buffer, "STATUS_AFTER_LAST_OUT": status,
             "PIPELINE_LOOKAHEAD": look, "OUTPUT_QUEUE_SIZE": size}
    end = m["stall_end"]
    expected = xfft_timing.status_clock(CASES["stall_end"]["FROM_END"], CASES["stall_end"]["LEN"], rules, False) \
        - CASES["stall_end"]["LEN"]
    if end["status_after_release"] != expected:
        raise ValueError(f"a {CASES['stall_end']['LEN']}-clock stall before sample N - "
                         f"{CASES['stall_end']['FROM_END']} gives the status word {end['status_after_release']} clocks "
                         f"after the output resumes, the status rule {expected} (with K = {look} and Q = {size})")
    # Input resume: the shortest release into a full FIFO that lets the input in, then as many samples in as
    # left; the input resumes INPUT_RESUME + (A - B) + (D - K) clocks after the release (D = FRAME_STARTED_DELAY
    # - INPUT_SLACK: the pipeline restarts A - B clocks after it, the input side sees the samples leave D - K
    # clocks late)
    taken = {k: m[f"release_{k}"]["release_in"] for k in RELEASES}
    resume = min((k for k, v in taken.items() if v > 0), default=None)
    if resume is None or resume == min(RELEASES) or any(v != (k if k >= resume else 0) for k, v in taken.items()):
        raise ValueError(f"releases of k = {min(RELEASES)} .. {max(RELEASES)} samples into a full input FIFO let "
                         f"{taken} samples in: not 'none below INPUT_RESUME, else k'")
    d = first["started"] - free
    if m["resume"]["resume_in"] + 1 - resume != halt_after - buffer + d - look:
        raise ValueError(f"after a blocked output, the input resumes {m['resume']['resume_in'] + 1} clocks after "
                         f"the release: not INPUT_RESUME {resume} + (A - B) {halt_after - buffer} + (D - K) "
                         f"{d - look}")
    return {
        "LATENCY_FIRST_OUT": lat,
        "LATENCY_LAST_OUT": lat_last,
        "INPUT_SLACK": free,
        "OUTPUT_STALL_COST": (stall["last_out"] - lat_last) // 100,
        "RESET_TO_READY": first["reset_to_data_ready"],
        "CONFIG_ACCEPT_LOADING": b2b["cfg1_accept"],
        "FRAME_BOUNDARY_STALL": b2b["first_in1"] - b2b["last_in0"] - 1,
        "STARVE_HALTS_OUTPUT": int(starve["first_out"] != lat),
        "OUTPUT_HALT_EVENT_AFTER": halt_after,
        "FRAME_STARTED_DELAY": first["started"],
        "STATUS_AFTER_LAST_OUT": status,
        "INPUT_LIMIT": blocked["accepted"],
        # A 100-clock stall in the middle of the frame: status word 100 - OUTPUT_BUFFER clocks late, so
        # STATUS_AFTER_LAST_OUT - OUTPUT_BUFFER clocks after the last output
        "OUTPUT_BUFFER": buffer,
        "INPUT_RESUME": resume,
        "PIPELINE_LOOKAHEAD": look,
        "OUTPUT_QUEUE_SIZE": size,
    }


def checks(case: str, v: dict, t: dict) -> list[tuple[str, int, int]]:
    """(what, measured, expected) of one case's results v under the constants t."""
    lat, lat_last = t["LATENCY_FIRST_OUT"], t["LATENCY_LAST_OUT"]
    out = []
    if case == "first_out":
        out += [("LATENCY_FIRST_OUT", v["first_out"], lat),
                ("RESET_TO_READY (s_axis_data_tready)", v["reset_to_data_ready"], t["RESET_TO_READY"]),
                ("RESET_TO_READY (s_axis_config_tready)", v["reset_to_cfg_ready"], t["RESET_TO_READY"]),
                ("RESET_TO_READY (first config handshake)", v["reset_to_cfg0_accept"], t["RESET_TO_READY"]),
                ("FRAME_STARTED_DELAY", v["started"], t["FRAME_STARTED_DELAY"])]
    elif case == "last_out":
        out += [("LATENCY_LAST_OUT", v["last_out"], lat_last),
                ("LATENCY_LAST_OUT = LATENCY_FIRST_OUT + N - 1", lat_last, lat + v["out_samples"] - 1),
                ("STATUS_AFTER_LAST_OUT", v["status"], t["STATUS_AFTER_LAST_OUT"])]
    elif case in PROBE_GAPS:
        gaps, slack_size = PROBE_GAPS[case], t["INPUT_SLACK"]
        cost, left = xfft_timing.gap_cost(gaps, slack_size)
        out.append((f"frame 0 first output with input gaps of {gaps} clocks (INPUT_SLACK)", v["first_out"],
                    lat + cost))
        if case in PROBE_IDLE:
            idle = PROBE_IDLE[case]
            start = xfft_timing.slack_after_idle(left, idle, slack_size)
            out.append((f"frame 1 first output after {idle} idle clocks (INPUT_SLACK, starts with slack {start})",
                        v["lat1_first"], lat - (slack_size - start)))
    elif case.startswith("gap_"):
        n, slack_size = v["gap_len"], t["INPUT_SLACK"]
        out += [(f"first output after a {n}-clock input gap (INPUT_SLACK)", v["first_out"],
                 lat + xfft_timing.gap_cost([n], slack_size)[0]),
                ("event_data_in_channel_halt clocks", v["in_halt_events"], max(n - slack_size, 0))]
    elif case.startswith("stall_end"):
        n, k, followed = v["stall_len"], CASES[case]["FROM_END"], CASES[case].get("FRAMES", 1) > 1
        out += [(f"last output after a {n}-clock output stall (OUTPUT_STALL_COST)", v["last_out"],
                 lat_last + t["OUTPUT_STALL_COST"] * n),
                (f"status word after the output resumes, a stall {k} samples before the end of "
                 f"{'a frame in a stream' if followed else 'the last frame'} (status rule, PIPELINE_LOOKAHEAD "
                 f"{xfft_timing.lookahead(t)}, OUTPUT_QUEUE_SIZE {xfft_timing.queue_size(t)})",
                 v["status_after_release"], xfft_timing.status_clock(k, n, t, followed) - n)]
    elif case.startswith("stall_"):
        n, after = v["stall_len"], t["OUTPUT_HALT_EVENT_AFTER"]
        out += [("first output (the stall comes later)", v["first_out"], lat),
                (f"last output after a {n}-clock output stall (OUTPUT_STALL_COST)", v["last_out"],
                 lat_last + t["OUTPUT_STALL_COST"] * n),
                ("status word from the last output (OUTPUT_BUFFER)", v["status"],
                 t["STATUS_AFTER_LAST_OUT"] - n + xfft_timing.status_delay([n], t)),
                ("event_data_out_channel_halt clocks (OUTPUT_HALT_EVENT_AFTER)", v["out_halt_events"],
                 max(n - after, 0))]
    elif case == "back_to_back":
        out += [("CONFIG_ACCEPT_LOADING", v["cfg1_accept"], t["CONFIG_ACCEPT_LOADING"]),
                ("FRAME_BOUNDARY_STALL", v["first_in1"] - v["last_in0"] - 1, t["FRAME_BOUNDARY_STALL"]),
                ("input stall clocks in both frames", v["in_stalls"], t["FRAME_BOUNDARY_STALL"])]
    elif case == "starve":
        out += [(f"frame 0 first output with a {v['max_gap']}-clock gap in frame 1 (STARVE_HALTS_OUTPUT)",
                 v["first_out"], lat + (v["max_gap"] if t["STARVE_HALTS_OUTPUT"] else 0)),
                ("event_data_in_channel_halt clocks", v["in_halt_events"], v["max_gap"] - t["INPUT_SLACK"])]
    elif case == "halt_1":
        out += [(f"frame 0 last output with {v['halt_gaps']} 1-clock gaps of frame 2 before it "
                 "(STARVE_HALTS_OUTPUT, INPUT_SLACK)", v["last_out"],
                 lat_last + (xfft_timing.gap_cost([1] * v["halt_gaps"], t["INPUT_SLACK"])[0]
                             if t["STARVE_HALTS_OUTPUT"] else 0))]
    elif case == "blocked":
        out += [("INPUT_LIMIT (samples accepted with the output blocked)", v["accepted"], t["INPUT_LIMIT"])]
    elif case == "resume":
        restart = t["OUTPUT_HALT_EVENT_AFTER"] - t["OUTPUT_BUFFER"]
        out += [("clocks from the first output handshake to the input's resume, less 1 (INPUT_RESUME + "
                 "(A - B) + (D - PIPELINE_LOOKAHEAD) - 1)", v["resume_in"],
                 t["INPUT_RESUME"] + restart + xfft_timing.pipeline_input_delay(t) - xfft_timing.lookahead(t) - 1)]
    elif case.startswith("release_"):
        k = CASES[case]["LEN"]
        out += [(f"samples in after a release of {k} into a full input FIFO (INPUT_RESUME)", v["release_in"],
                 k if k >= t["INPUT_RESUME"] else 0)]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "measure", "speed", "probe"))
    parser.add_argument("cases", nargs="*", help="validate: only these cases (default: all); "
                                                 "probe: the gap/stall patterns")
    parser.add_argument("--model", default=os.environ.get("FFT_MODEL") or "rtl", help=" or ".join(MODELS))
    parser.add_argument("-j", "--jobs", type=int, default=0, help="parallel processes (default: one per case)")
    parser.add_argument("--frames", type=int, help="speed (default 20) and probe (default 1): frames")
    args = parser.parse_intermixed_args()
    if args.model == "cmodel":
        parser.error("--model cmodel (FFT_MODEL=cmodel) is no longer supported: the stand-in is --model engine")
    if args.model not in MODELS:
        parser.error(f"--model {args.model}: one of {', '.join(MODELS)}")
    cfg = xfft_config.current()
    log(f"configuration {cfg.tag}")

    if args.command == "speed":
        args.frames = args.frames or 20
        v = run_cases(cfg, args.model, {"speed": {"CASE": "speed", "FRAMES": args.frames}}, 1)["speed"]
        log(f"speed on {args.model}: {v['clocks']} clocks in {v['wall_s']:.2f} s simulation wall time = "
            f"{v['clocks'] / max(v['wall_s'], 1e-9):.1f} clocks/s ({args.frames} frames of {cfg.points} points)")
        return 0

    if args.command == "probe":
        if not args.cases:
            parser.error("probe needs at least one pattern, e.g. g256:1,g512:5")
        runs = {}
        for i, spec in enumerate(args.cases):
            plus, kinds = {"CASE": "probe", "FRAMES": args.frames or 1}, {"g": 0, "s": 0}
            for item in filter(None, spec.split(",")):
                m = re.fullmatch(r"([gs])(\d+):(\d+)", item.strip())
                if not m or kinds[m[1]] == 4:
                    parser.error(f"{item}: expected g<sample>:<clocks> or s<output>:<clocks>, up to 4 of each")
                key = "GAP" if m[1] == "g" else "STALL"
                plus[f"{key}POS{kinds[m[1]]}"], plus[f"{key}LEN{kinds[m[1]]}"] = int(m[2]), int(m[3])
                kinds[m[1]] += 1
            runs[f"probe_{i}"] = plus
        m = run_cases(cfg, args.model, runs, args.jobs)
        for i, spec in enumerate(args.cases):
            v = m[f"probe_{i}"]
            lat = [(v["first_out"], v["last_out"]), (v["lat1_first"], v["lat1_last"]),
                   (v["lat2_first"], v["lat2_last"])][:min(args.frames or 1, 3)]
            print(f"probe {spec}: latency first/last out per frame {' '.join(f'{a}/{b}' for a, b in lat)} "
                  f"(gaps {v['gap_len']} clocks, stalls {v['stall_len']} clocks), halt events in "
                  f"{v['in_halt_events']}, out {v['out_halt_events']}")
        return 0

    if args.command == "measure":
        if args.model != "rtl":
            parser.error("measure runs on the real core (--model rtl)")
        m = run_cases(cfg, "rtl", {c: CASES[c] for c in MEASURE_CASES}, args.jobs)
        try:
            t = derive(m)
        except ValueError as e:
            log(f"the core does not follow the stand-in's timing rules: {e}")
            return 1
        path = xfft_timing.measured_file(cfg)
        path.write_text(json.dumps(t, indent=1) + "\n")
        log(f"wrote {path}")
        if cfg.tag in xfft_timing.TABLE and xfft_timing.TABLE[cfg.tag] != t:
            log("note: TABLE in model/xfft_timing.py has other values for this configuration, and TABLE wins")
        n = cfg.points
        print(f"\nTABLE entry for model/xfft_timing.py:\n\n"
              f"    # {cfg.spec or 'default configuration'}: N = {n}: LATENCY_FIRST_OUT = 2N + "
              f"{t['LATENCY_FIRST_OUT'] - 2 * n}, INPUT_LIMIT = 2N + {t['INPUT_LIMIT'] - 2 * n}\n"
              f"    \"{cfg.tag}\": {{\n" + "".join(f"        \"{k}\": {v},\n" for k, v in t.items()) + "    },\n")
        log("validate them with: xfft_timing_bench.py validate")
        return 0

    cases = all_cases(cfg.points)
    unknown = set(args.cases) - set(cases)
    if unknown:
        parser.error(f"unknown cases: {' '.join(sorted(unknown))}")
    t = xfft_timing.constants(cfg)
    names = args.cases or list(cases)
    m = run_cases(cfg, args.model, {c: cases[c] for c in names}, args.jobs)
    failed = []
    for case in names:
        for what, measured, expected in checks(case, m[case], t):
            ok = measured == expected
            print(f"{'PASS' if ok else 'FAIL'} {case}: {what}: measured {measured}, expected {expected}")
            if not ok:
                failed.append(case)
    failed = sorted(set(failed))
    log(f"validate on {args.model}: {len(names)} cases, failed: {' '.join(failed) if failed else 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
