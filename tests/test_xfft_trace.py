"""The C engine on every simulator, clock by clock, through traces: the same engine must give the same outputs
through each wrapper on each simulator.

The core is the toplevel: the C engine on XSim and Verilator (its SV wrapper), NVC and GHDL (its VHDL wrapper),
wherever the simulator is installed; XSim's traces are the reference. Each writes its traces into its test
directory. The clock starts the same way on every simulator (start_clock()): low from time
0, the inputs driven from 1 ns, the first rising edge at 3 ns; nothing before the first edge is traced (Verilator
has no falling edge at time 0 and 0 where the others have X before the first edge).
- trace.txt: xfft_chaos.py's traffic (fixed seed), one line per clock: the inputs of a rising edge and every
  output after it. Not on Verilator: it is 2-state, so the X and Z of the traffic would mean nothing there;
- trace_2state.txt: the same traffic without X and Z (xfft_chaos.two_state_segments()), on every simulator;
- trace_cases.txt: every variation of the characterization (characterize/bench/cases.py, with the extra ones),
  each from its own reset, driven by xfft_tb.py's Lane; one line per rising edge from the second one on, with
  every output before it (the inputs are left out: they follow from the outputs). cases_index.json has the first
  clock of each case.
Each run also checks the chaos traffic on its own: it runs to the end with output and status handshakes, and every
output is known (no X or Z) from the first edge on, whatever the inputs.
The traffic reacts to the core's tready, so equal outputs mean equal traffic. test_xfft_trace_compare then
compares every other simulator's trace with XSim's, line by line, and names the first difference. The simulators
may be in different installations: run this file where XSim is (it writes the reference), then where NVC, GHDL
and Verilator are, with the same build/ directory; the comparison runs in each and compares what is there (it is
skipped without the reference). With pytest -n, run the comparison on its own afterwards (pytest
tests/test_xfft_trace.py -k compare). Beside each trace, model.sha256 has the digest of the model that wrote it;
the comparison leaves out a trace whose model has changed since (run test_xfft_trace again).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import cocotb
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

import hdl_sources
import sim_runner
import xfft_chaos
import xfft_config
import xfft_tb
from xfft_tb import EVENTS, Lane

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "characterize" / "bench"))
import cases  # noqa: E402

PERIOD_NS = 4
SEED = 11
TRACE = "trace.txt"
TRACE_2STATE = "trace_2state.txt"
CASES_TRACE = "trace_cases.txt"
CASES_INDEX = "cases_index.json"
TRACES = (TRACE, TRACE_2STATE, CASES_TRACE)
# The simulators without X and Z (no trace.txt there)
TWO_STATE = ("verilator",)
REFERENCE = "vivado_xfft_engine"        # the build directory of the engine on XSim
MODEL_DIGEST = "model.sha256"
# The fields of a trace line (xfft_chaos.trace_line()); a field of single bits lists them
FIELDS = (("clock",), xfft_chaos.BITS, ("s_axis_config_tdata",), ("s_axis_data_tdata",), xfft_chaos.OUTPUTS[:5],
          ("m_axis_data_tdata",), ("m_axis_data_tuser",), ("m_axis_status_tdata",), EVENTS)


def traffic_id() -> str:
    """The first line of a trace: which traffic made it (the traffic's code, the seed, the configuration)."""
    # This bench shapes the traces too (the clock start, the first-edge skip, the test order)
    code = b"".join(Path(f).read_bytes() for f in (__file__, *(m.__file__ for m in (xfft_chaos, xfft_config,
                                                                                   xfft_tb, cases))))
    return f"# traffic {hashlib.sha256(code).hexdigest()[:16]} seed {SEED} {xfft_config.current().tag}\n"


def model_digest() -> str:
    """The digest of the engine's files (its build script and inputs)."""
    dpi = hdl_sources.xfft_engine(xfft_config.current())
    h = hashlib.sha256()
    for f in (dpi.build, *dpi.inputs):
        h.update(Path(f).read_bytes())
    return h.hexdigest()


def traces(sim: str) -> list[str]:
    """The traces a simulator writes."""
    return [name for name in TRACES if not (name == TRACE and sim in TWO_STATE)]


def two_state() -> bool:
    """Whether the simulator of this cocotb run is 2-state (outside a simulation cocotb has no SIM_NAME: not)."""
    return (getattr(cocotb, "SIM_NAME", None) or "").lower().startswith(TWO_STATE)


async def start_clock(dut) -> None:
    """aclk low from time 0 and the clock started at 1 ns, low for its first half period: the first rising edge
    at 3 ns. Returns at 1 ns, so the inputs are driven after any edge of time 0 (XSim, NVC and GHDL see aclk
    fall from unknown at time 0, Verilator does not) and before the first rising edge, on every simulator."""
    dut.aclk.value = 0
    await Timer(1, "ns")
    Clock(dut.aclk, PERIOD_NS, unit="ns").start(start_high=False)


async def chaos_trace(dut, name: str, segments: list) -> dict:
    """xfft_chaos.py's traffic over the segments, every clock into the trace name."""
    cfg = xfft_config.current()
    await start_clock(dut)
    with open(name, "w") as f:
        f.write(traffic_id())
        counts = await xfft_chaos.Chaos(dut, cfg, SEED, trace=f).run(dut.aclk, segments)
    dut._log.info("%s: %s", name, counts)
    assert counts["out_hs"] > 0 and counts["status_hs"] > 0, f"no output: {counts}"
    assert counts["unknown_out"] == 0, f"outputs unknown after the first edge: {counts}"
    return counts


async def monitor(dut, f, state: dict) -> None:
    """Every output before each rising edge, one line per edge from the second edge on (the fields of
    xfft_chaos.trace_line()): before the first edge the simulators differ (X, or 0 on Verilator)."""
    ports = {name: getattr(dut, name) for name in xfft_chaos.OUTPUTS}
    o = xfft_chaos.OUTPUTS
    await RisingEdge(dut.aclk)
    while True:
        await RisingEdge(dut.aclk)
        out = {name: xfft_chaos.hexstr(p.value) for name, p in ports.items()}
        f.write(" ".join([str(state["clock"]), "-", "-", "-", "".join(out[n] for n in o[:5]),
                          *(out[n] for n in o[5:8]), "".join(out[e] for e in EVENTS)]) + "\n")
        state["clock"] += 1


# In this order on every simulator, trace last: it is skipped on Verilator, and a core's data outputs keep their
# values through a reset, so every trace must follow the same traffic everywhere
@cocotb.test()
async def characterization_cases(dut):
    """Every characterization case, every edge into trace_cases.txt."""
    cfg = xfft_config.current()
    await start_clock(dut)
    state, index = {"clock": 0}, {}
    with open(CASES_TRACE, "w") as f:
        f.write(traffic_id())
        lane = Lane(dut, cfg)
        lane.idle()
        dut.aresetn.value = 0
        cocotb.start_soon(monitor(dut, f, state))
        for case in cases.cases(cfg.points, extra=True).values():
            lane.idle()
            dut.aresetn.value = 0
            await ClockCycles(dut.aclk, 10)
            index[case.name] = state["clock"]
            dut.aresetn.value = 1
            await lane.run(dut.aclk, xfft_tb.case_frames(cfg, case), timeout=(case.frames + 12) * 6 * cfg.points)
    Path(CASES_INDEX).write_text(json.dumps(index, indent=1))
    dut._log.info("%d cases, %d clocks", len(index), state["clock"])


@cocotb.test()
async def trace_2state(dut):
    """xfft_chaos.py's traffic without X and Z, every clock into trace_2state.txt."""
    counts = await chaos_trace(dut, TRACE_2STATE, xfft_chaos.two_state_segments(xfft_config.current()))
    assert counts["unknown"] == 0


@cocotb.skipif(two_state(), reason="a 2-state simulator: the traffic's X and Z mean nothing there "
                                    "(trace_2state has the same traffic without them)")
@cocotb.test()
async def trace(dut):
    """xfft_chaos.py's traffic, every clock into trace.txt."""
    counts = await chaos_trace(dut, TRACE, xfft_chaos.segments(xfft_config.current()))
    assert counts["unknown"] > 0


@pytest.mark.parametrize("sim", sim_runner.simulators(*hdl_sources.SIMULATORS))
def test_xfft_trace(sim: str) -> None:
    cfg = xfft_config.current()
    core = hdl_sources.xfft_engine(cfg)
    build_name = f"{cfg.tag}/xfft_trace"
    sim_runner.run(
        sim=sim,
        sources=[core],
        hdl_toplevel=cfg.module,
        hdl_toplevel_lang=hdl_sources.toplevel_lang("engine", sim),
        test_module=__name__,
        build_name=build_name,
        verilog_defines={"XFFT_CORE_NAME": cfg.module},
        extra_env={"XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
        cache=True,
        cache_outputs=[*traces(sim), CASES_INDEX],
    )
    (sim_runner.SIM_BUILD / build_name / f"{sim}_{core.name}" / MODEL_DIGEST).write_text(model_digest())


def first_difference(ref: Path, other: Path) -> str | None:
    """None when the traces are equal (case-insensitive), else the first differing line and its ports."""
    with open(ref) as a, open(other) as b:
        for number, (la, lb) in enumerate(zip(a, b)):
            fa, fb = la.lower().split(), lb.lower().split()
            if fa == fb:
                continue
            ports = []
            for names, va, vb in zip(FIELDS, fa, fb):
                if va != vb:
                    ports += [n for i, n in enumerate(names) if len(names) == 1 or va[i:i + 1] != vb[i:i + 1]]
            return (f"line {number + 1}: {', '.join(ports) or 'format'}\n  reference {la.strip()}\n"
                    f"  {other.parent.name} {lb.strip()}")
        rest_a, rest_b = a.readline(), b.readline()
        if rest_a or rest_b:
            return f"{'reference' if rest_a else other.parent.name} is longer"
    return None


def test_xfft_trace_compare() -> None:
    """Every other simulator's trace there is against XSim's; a trace of other traffic (an older xfft_chaos.py)
    or of an older engine is left out with a note."""
    root = sim_runner.SIM_BUILD / xfft_config.current().tag / "xfft_trace"
    if not (root / REFERENCE / TRACE).is_file():
        pytest.skip(f"no reference trace in {(root / REFERENCE).relative_to(sim_runner.REPO)}: run test_xfft_trace "
                    f"on XSim first")
    digest = model_digest()

    def current(build_dir: Path) -> bool:
        stamp = build_dir / MODEL_DIGEST
        return stamp.is_file() and stamp.read_text() == digest

    if not current(root / REFERENCE):
        pytest.skip(f"the reference trace in {REFERENCE} was made by another version of the engine: run "
                    f"test_xfft_trace on XSim again")
    failures, compared = [], 0
    for name in TRACES:
        ref = root / REFERENCE / name
        if not ref.is_file():
            print(f"{name}: left out, the reference has no {name}: run test_xfft_trace on XSim again")
            continue
        with open(ref) as f:
            traffic = f.readline()
        lines = sum(1 for _ in open(ref)) - 1
        for path in sorted(p for p in root.glob(f"*/{name}") if p.parent.name != REFERENCE):
            if not current(path.parent):
                print(f"{path.parent.name}/{name}: left out, made by an older engine: run test_xfft_trace on it "
                      f"again")
                continue
            with open(path) as f:
                if f.readline() != traffic:
                    print(f"{path.parent.name}/{name}: left out, made with other traffic than the reference: run "
                          f"test_xfft_trace on it again")
                    continue
            compared += 1
            diff = first_difference(ref, path)
            print(f"{path.parent.name}/{name}: {'identical' if diff is None else 'DIFFERS'} ({lines} clocks)")
            if diff is not None:
                failures.append(f"{path.parent.name}/{name}: {diff}")
    assert not failures, "\n".join(failures)
    if not compared:
        pytest.skip("no trace of another simulator with the reference's traffic yet")
