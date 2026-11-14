"""The stand-in's timing against the real core's, from stored reference results: no Vivado needed.

tests/reference/timing_reference.bin.xz holds what the real core did (XSim) in the variations of the
characterization (characterize/bench/cases.py, with the extra ones) measured on each configuration: the clocks
of tready after reset and of every input, output, config and status handshake, counted from the first clock edge
with aresetn high, and the constants measured on that core. The format is binary (tests/reference_codec/
refcodec.h: runs of consecutive clocks, variable-length integers), compressed with xz; the test builds
tests/reference_codec/refcodec_unpack.cpp with a C++ compiler (CXX, else c++ or g++) to read it. This test runs the same
variations on the stand-in, built timing-only (scripts/build_engine.py --timing-only: the C engine without AMD's
C model, so no Vivado and no generated core; every output sample is 0), on every installed simulator, and compares
those clocks. Data, OVFLO and the status words' values are not compared: they come from AMD's C model.

One simulation runs every variation of a configuration in turn, each from its own reset (aresetn low for 30
clocks), driven as the characterization bench drives them (its Lane and frames). The stand-in is built with the
constants measured on the same real core (the file's "timing", through XFFT_TIMING_JSON), else with those of
model/xfft_timing.py; a configuration with neither is skipped (too few cases were measured on it to derive them).

tests/reference/known_differences.json lists the (variation, field) pairs where the stand-in is known to differ
from the core, with the reason; they are reported and do not fail. Every other difference fails. Run it anywhere
with a C compiler and one of the simulators (CI runs it on the open-source ones):

    pytest tests/test_timing_reference.py
    XFFT_TIMING_CASES=blocked,dense_backpressure pytest tests/test_timing_reference.py    # only these variations
    XFFT_TIMING_TAGS=all pytest tests/test_timing_reference.py                            # every configuration
    XFFT_TIMING_REPORT=dir pytest ...       # also every difference of each configuration into dir/<sim>_<tag>.json

Which configurations: XFFT_TIMING_TAGS, a comma-separated list of tags or shell patterns (n8192_*), or all; by
default those that tests/reference/known_differences.json lists (checked ones, also with no known difference).
The reference holds every configuration measured on the real core; the default keeps a run (CI) short.
"""

from __future__ import annotations

import fnmatch
import json
import lzma
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import cocotb
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

import hdl_sources
import sim_runner
import xfft_config
import xfft_timing
from xfft_tb import Lane

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "characterize" / "bench"))
import cases  # noqa: E402
from test_characterize import Monitor, frames_of  # noqa: E402

REFERENCE = REPO / "tests" / "reference"
KNOWN = REFERENCE / "known_differences.json"
FACTS = "timing_facts.json"
PERIOD_NS = 10
RESET_CLOCKS = 30


def selected(names) -> list[str]:
    """The variations to run: XFFT_TIMING_CASES (comma separated), else all."""
    only = [c.strip() for c in os.environ.get("XFFT_TIMING_CASES", "").split(",") if c.strip()]
    return [n for n in names if not only or n in only]


# The fields stored as runs of consecutive clocks; "tready" is stored as it is (two clocks)
RUN_FIELDS = ("input handshakes", "output handshakes", "config handshakes", "status clocks")


REFERENCE_FILE = REFERENCE / "timing_reference.bin.xz"
CODEC = REPO / "tests" / "reference_codec"


def unpacker() -> Path | None:
    """tests/reference_codec/refcodec_unpack, built once into build/ (None without a C++ compiler)."""
    cxx = shlex.split(os.environ["CXX"]) if os.environ.get("CXX") else next(
        ([c] for c in ("c++", "g++") if shutil.which(c)), None)
    if cxx is None:
        return None
    exe = REPO / "build" / "reference_codec" / "refcodec_unpack"
    sources = [CODEC / "refcodec_unpack.cpp", CODEC / "refcodec.h"]
    if not exe.is_file() or any(p.stat().st_mtime > exe.stat().st_mtime for p in sources):
        exe.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{CODEC}", "-o", str(exe),
                        str(CODEC / "refcodec_unpack.cpp")], check=True)
    return exe


def unpack(tags: list[str] | None = None) -> dict:
    """The reference of each configuration (all, or tags): {tag: {xfft_config, source, timing?, cases}}, the
    handshake clocks as runs [first clock, count] (decode() expands them)."""
    exe = unpacker()
    if exe is None:
        return {}
    text = subprocess.run([str(exe), *(tags or [])], input=lzma.decompress(REFERENCE_FILE.read_bytes()),
                          capture_output=True, check=True).stdout.decode()
    docs, cur, case = {}, None, None
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        if kind == "config":
            tag, spec = rest.split(" ")
            cur = docs[tag] = {"xfft_config": spec, "cases": {}}
        elif kind == "source":
            cur["source"] = rest
        elif kind == "timing":
            name, value = rest.split(" ")
            cur.setdefault("timing", {})[name] = int(value)
        elif kind == "case":
            case = cur["cases"][rest] = {}
        elif kind == "tready":
            case["tready"] = [int(x) for x in rest.split(" ")]
        elif kind == "runs":
            parts = [int(x) for x in rest.split(" ")]
            case[RUN_FIELDS[parts[0]]] = [list(p) for p in zip(parts[1::2], parts[2::2])]
    return docs


def all_tags() -> list[str]:
    exe = unpacker()
    if exe is None:
        return []
    text = subprocess.run([str(exe)], input=lzma.decompress(REFERENCE_FILE.read_bytes()), capture_output=True,
                          check=True).stdout.decode()
    return sorted(line.split(" ")[1] for line in text.splitlines() if line.startswith("config "))


def reference_tags() -> list[str]:
    """The configurations to test (module docstring: XFFT_TIMING_TAGS)."""
    every = all_tags()
    want = os.environ.get("XFFT_TIMING_TAGS", "").strip()
    if want == "all":
        return every
    if not want:
        checked = json.loads(KNOWN.read_text()) if KNOWN.is_file() else {}
        return [t for t in every if t in checked]
    patterns = [w.strip() for w in want.split(",") if w.strip()]
    return [t for t in every if any(fnmatch.fnmatchcase(t, p) for p in patterns)]


def decode(fields: dict) -> dict:
    """One variation of a reference file with its runs expanded into clock lists."""
    return {f: [first + i for first, count in v for i in range(count)] if f in RUN_FIELDS else v
            for f, v in fields.items()}


def timing(facts: dict) -> dict:
    """The compared clocks of one run (bench facts), counted from the first edge with aresetn high."""
    r = facts["reset_edge"]
    return {"tready": [facts["data_tready_first"] - r, facts["cfg_tready_first"] - r],
            "input handshakes": [c - r for c in facts["in_hs"]],
            "output handshakes": [c - r for c in facts["out_hs"]],
            "config handshakes": [c - r for c in facts["cfg_hs"]],
            "status clocks": [s[0] - r for s in facts["status_hs"]]}


def first_difference(a: list, b: list) -> str | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"#{i}: core {x}, stand-in {y}"
    return f"length: core {len(a)}, stand-in {len(b)}" if len(a) != len(b) else None


@cocotb.test()
async def timing_reference(dut):
    """Every variation of the configuration in turn, each from its own reset; their clocks into FACTS."""
    cfg = xfft_config.current()
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    out = {}
    every = cases.cases(cfg.points, extra=True)
    for name in selected(every):
        case = every[name]
        lane = Lane(dut, cfg)
        lane.idle()
        dut.aresetn.value = 0
        monitor = Monitor(dut)
        cocotb.start_soon(monitor.run())
        await ClockCycles(dut.aclk, RESET_CLOCKS)
        dut.aresetn.value = 1
        await lane.run(dut.aclk, frames_of(cfg, case), timeout=(case.frames + 12) * 6 * cfg.points)
        await ClockCycles(dut.aclk, 2)
        monitor.done = True
        await ClockCycles(dut.aclk, 1)
        out[name] = timing(monitor.facts)
    Path(FACTS).write_text(json.dumps(out))
    dut._log.info("%d variations on %s", len(out), cfg.tag)


@pytest.mark.parametrize("tag", reference_tags())
@pytest.mark.parametrize("sim", hdl_sources.simulators("engine"))
def test_timing_reference(sim: str, tag: str, monkeypatch, tmp_path) -> None:
    ref = unpack([tag])[tag]
    monkeypatch.setenv("XFFT_CONFIG", ref["xfft_config"])
    cfg = xfft_config.current()
    assert cfg.tag == tag, f"{tag}: XFFT_CONFIG {ref['xfft_config']} gives {cfg.tag}"
    if "timing" in ref:
        constants = tmp_path / f"{tag}.json"
        constants.write_text(json.dumps({"constants": ref["timing"]}))
        monkeypatch.setenv("XFFT_TIMING_JSON", str(constants))
    else:
        try:
            xfft_timing.constants(cfg)
        except LookupError:
            pytest.skip(f"{tag}: no timing constants (too few cases measured on it to derive them)")
    core = hdl_sources.xfft_engine(cfg, timing_only=True)
    build_name = f"timing_reference/{tag}"
    sim_runner.run(sources=[core], hdl_toplevel=cfg.module, test_module=__name__, build_name=build_name, sim=sim,
                   hdl_toplevel_lang=hdl_sources.toplevel_lang("engine", sim),
                   verilog_defines={"XFFT_CORE_NAME": cfg.module},
                   extra_env={"XFFT_CONFIG": ref["xfft_config"],
                              "XFFT_TIMING_CASES": os.environ.get("XFFT_TIMING_CASES", "")})
    got = json.loads((sim_runner.SIM_BUILD / build_name / f"{sim}_{core.name}" / FACTS).read_text())
    known = json.loads(KNOWN.read_text()).get(tag, {}) if KNOWN.is_file() else {}
    failures, notes = [], []
    for name in selected(ref["cases"]):
        want = decode(ref["cases"][name])
        if name not in got:
            failures.append(f"{name}: not run (cases.py has no such variation)")
            continue
        for field, clocks in want.items():
            diff = first_difference(clocks, got[name][field])
            expected = field in known.get(name, {})
            if diff and expected:
                notes.append(f"{name}: {field}: {diff} (known: {known[name][field]})")
            elif diff:
                failures.append(f"{name}: {field}: {diff}")
            elif expected:
                notes.append(f"{name}: {field}: identical now (listed as a known difference)")
    for n in notes:
        print(f"known  {n}")
    report = os.environ.get("XFFT_TIMING_REPORT", "").strip()
    if report:
        Path(report).mkdir(parents=True, exist_ok=True)
        (Path(report) / f"{sim}_{tag}.json").write_text(json.dumps(
            {"sim": sim, "tag": tag, "cases": len(selected(ref["cases"])), "failures": failures, "known": notes},
            indent=1))
    assert not failures, f"{len(failures)} timing difference(s) on {sim}, {tag}:\n  " + "\n  ".join(failures)
