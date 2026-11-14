"""Several stand-ins in one simulation: two instances of one configuration and one instance of another, each
configuration from its own library (scripts/build_engine.py names the C functions, the package and the wrapper
after the configuration's module), on every installed simulator. No Vivado: the stand-ins are built timing-only
(scripts/build_engine.py --timing-only), with the constants in timing/ (CONFIG_B's under the tag of its
configuration with module xfft_core: model/xfft_timing.py looks them up there).

The toplevel (tests/xfft_multi.sv on XSim, Verilator and Riviera, tests/xfft_multi.vhdl on NVC and GHDL) holds a0
and a1, two instances of CONFIG_A (module xfft_core: the default names), and b, an instance of CONFIG_B (module
xfft_b). Each instance gets its own light traffic (TRAFFIC: a few frames with input gaps and output stalls), driven
through its own ports (xfft_tb.Lane with the instance's prefix). One simulation runs the three at once, then each
alone (the others idle), each phase from its own reset. Two more simulations run each configuration on its own,
as the only core of the simulation, with the same traffic. Every instance's LaneResult (the clock of every
handshake and event, the output and status words) must be the same in all of them. A last phase of the shared
simulation sends one frame without gaps or stalls to every instance at once: each must come out after its own
configuration's LATENCY_FIRST_OUT (so that a model with the other configuration's constants does not pass).

With XFFT_MULTI_CMODEL=1 the stand-ins are full builds with the C model (Vivado's IP C model, as
scripts/build_engine.py builds it), the frames carry random data (a seed per traffic), and every output frame is
also checked against a floating-point FFT (xfft_tb.check_frame); the comparison with each configuration alone then
covers the output words too.

XFFT_MULTI_A and XFFT_MULTI_B replace the two configurations (XFFT_CONFIG syntax; the module is set to xfft_core
and xfft_b, the toplevel's). A configuration without constants in model/xfft_timing.py (TABLE, timing/) takes those
measured on the real core from the timing reference (tests/reference/), or the test is skipped.

    pytest tests/test_xfft_multi.py
    XFFT_MULTI_CMODEL=1 pytest tests/test_xfft_multi.py
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import cocotb
import numpy as np
import pytest
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

import hdl_sources
import sim_runner
import xfft_config
import xfft_timing
from xfft_tb import Frame, Lane, check_frame, random_frame, schedule_a, schedule_b

HERE = Path(__file__).resolve().parent
CONFIG_A = dataclasses.replace(xfft_config.parse(os.environ.get("XFFT_MULTI_A") or "nfft=8"),
                               module=xfft_config.DEFAULT_MODULE)
CONFIG_B = dataclasses.replace(xfft_config.parse(os.environ.get("XFFT_MULTI_B") or
                                                 "nfft=9,input_width=18,twiddle_width=18,rounding=truncation,"
                                                 "clock_mhz=250"), module="xfft_b")
# The traffic of each instance: per frame, {input sample: clocks of s_axis_data_tvalid low before it} and {output
# sample: clocks of m_axis_data_tready low before it}
TRAFFIC = {
    "a0": {"frames": 3, "gaps": {0: {5: 1, 100: 3}, 1: {128: 40}}, "stalls": {0: {10: 2}, 2: {128: 50}}},
    "a1": {"frames": 3, "gaps": {1: {1: 2}, 2: {200: 7}}, "stalls": {1: {0: 5, 64: 300}}},
    "b": {"frames": 3, "gaps": {0: {256: 2}, 2: {3: 1, 400: 20}}, "stalls": {0: {300: 30}, 1: {5: 1}}},
}
# One frame without gaps or stalls per instance, for the absolute latency
CLEAN = {f"{name}_clean": {"frames": 1, "gaps": {}, "stalls": {}} for name in TRAFFIC}
TRAFFIC |= CLEAN
# The instances of the toplevel: port prefix -> (configuration, traffic)
INSTANCES = {"a0_": (CONFIG_A, "a0"), "a1_": (CONFIG_A, "a1"), "b_": (CONFIG_B, "b")}
RESULTS = "multi.json"
PERIOD_NS = 10
RESET_CLOCKS = 30
CMODEL = os.environ.get("XFFT_MULTI_CMODEL", "") == "1"     # the C model and random data, see above


def frames(cfg: xfft_config.XfftConfig, name: str) -> list[Frame]:
    """The frames of traffic `name`: zero data (the timing-only stand-in puts out zeros anyway), or with CMODEL
    random data from a seed of the traffic's own, forward and inverse alternating, both scaling schedules."""
    traffic = TRAFFIC[name]
    rng = np.random.default_rng(sorted(TRAFFIC).index(name) + 1)
    return [Frame(random_frame(cfg, rng) if CMODEL else np.zeros(cfg.points, complex), f % 2 == 0,
                  schedule_a(cfg) if f % 3 != 2 else schedule_b(cfg),
                  dict(traffic["gaps"].get(f, {})), dict(traffic["stalls"].get(f, {})))
            for f in range(traffic["frames"])]


def tolerance(cfg: xfft_config.XfftConfig) -> tuple[float, float]:
    """(rms, max) error in LSB allowed against the floating-point FFT: xfft_tb.check_frame's defaults, wider with
    truncation, whose bias adds up over the stages (measured on N = 512 to 4096: rms up to 1.8, max up to 17 LSB).
    The bound only catches output that is not this configuration's transform; the comparison with the
    configuration alone checks the words exactly."""
    if cfg.rounding == "truncation":
        return 1.0 + cfg.nfft / 4, 4.0 * cfg.nfft
    return 2.0, 8.0


def plain(result) -> dict:
    """A LaneResult as JSON values (tuples become lists), for the results file and the comparison."""
    return json.loads(json.dumps(dataclasses.asdict(result)))


@cocotb.test()
async def multi_lanes(dut):
    """The phases of XFFT_MULTI in turn, each from its own reset: every lane of a phase runs its traffic at the same
    time, the toplevel's other lanes idle. Writes the LaneResult of each lane of each phase into RESULTS."""
    spec = json.loads(os.environ["XFFT_MULTI"])
    configs = {prefix: xfft_config.parse(s) for prefix, s in spec["lanes"].items()}
    cocotb.start_soon(Clock(dut.aclk, PERIOD_NS, unit="ns").start())
    out = []
    for phase in spec["phases"]:
        lanes = {prefix: Lane(dut, cfg, prefix=prefix) for prefix, cfg in configs.items()}
        for lane in lanes.values():
            lane.idle()
        dut.aresetn.value = 0
        await ClockCycles(dut.aclk, RESET_CLOCKS)
        dut.aresetn.value = 1
        sent = {name: frames(configs[prefix], name) for prefix, name in phase}
        tasks = {name: cocotb.start_soon(lanes[prefix].run(dut.aclk, sent[name])) for prefix, name in phase}
        results = {name: await task for name, task in tasks.items()}
        if CMODEL:
            for prefix, name in phase:
                for f, (frame, got) in enumerate(zip(sent[name], results[name].frames)):
                    check_frame(configs[prefix], frame, got, dut._log, f"{prefix or '-'}{name} frame {f}",
                                *tolerance(configs[prefix]))
        out.append({name: plain(result) for name, result in results.items()})
        await ClockCycles(dut.aclk, 5)
    Path(RESULTS).write_text(json.dumps(out))
    dut._log.info("%d phases: %s", len(out), ", ".join("+".join(p) for p in out))


def constants_of(cfg: xfft_config.XfftConfig, tmp_path: Path) -> tuple[dict, Path | None]:
    """cfg's constants and the file to build its stand-in with: None when model/xfft_timing.py has them, else the
    timing reference's (written into tmp_path); skips the test when neither has them."""
    try:
        return xfft_timing.constants(cfg), None
    except LookupError:
        pass
    import test_timing_reference

    tag = dataclasses.replace(cfg, module=xfft_config.DEFAULT_MODULE).tag
    measured = test_timing_reference.unpack([tag]).get(tag, {}).get("timing")
    if measured is None:
        pytest.skip(f"{tag}: no timing constants, neither in model/xfft_timing.py nor in the timing reference")
    path = tmp_path / f"{cfg.module}.json"
    path.write_text(json.dumps({"constants": measured}))
    return measured, path


def stand_in(cfg: xfft_config.XfftConfig, constants: Path | None) -> sim_runner.DpiModel:
    """The stand-in of cfg: timing-only, or with CMODEL the C model too."""
    return hdl_sources.xfft_engine(cfg, timing_only=not CMODEL, constants=constants)


def simulate(sim: str, name: str, sources: list, toplevel: str, lanes: dict, phases: list) -> list[dict]:
    """One simulation of multi_lanes: lanes {prefix: configuration}, phases [[(prefix, traffic), ...], ...]; the
    results of each phase, {traffic: LaneResult as JSON values}."""
    models = [s for s in sources if isinstance(s, sim_runner.DpiModel)]
    build_name = f"xfft_multi/{name}"
    spec = {"lanes": {prefix: cfg.spec for prefix, cfg in lanes.items()}, "phases": phases}
    sim_runner.run(sources=sources, hdl_toplevel=toplevel, test_module=__name__, build_name=build_name, sim=sim,
                   hdl_toplevel_lang=hdl_sources.toplevel_lang("engine", sim),
                   extra_env={"XFFT_MULTI": json.dumps(spec)})
    build_dir = sim_runner.SIM_BUILD / build_name / "_".join([sim, *dict.fromkeys(m.name for m in models)])
    return json.loads((build_dir / RESULTS).read_text())


def difference(want: dict, got: dict) -> str | None:
    """The first field in which two LaneResults differ, or None."""
    for field, value in want.items():
        if got.get(field) != value:
            if field == "frames":
                for f, (a, b) in enumerate(zip(value, got[field])):
                    diff = {k: (a[k], b[k]) for k in a if a[k] != b[k] and k not in ("data", "user", "last")}
                    if a != b:
                        return f"frame {f}: {diff or 'output words'}"
                return f"frames: {len(value)} alone, {len(got[field])} here"
            return f"{field}: {value} alone, {got.get(field)} here"
    return None


@pytest.mark.parametrize("sim", hdl_sources.simulators("engine"))
def test_xfft_multi(sim: str, tmp_path: Path) -> None:
    (constants_a, file_a), (constants_b, file_b) = constants_of(CONFIG_A, tmp_path), constants_of(CONFIG_B, tmp_path)
    latency = {CONFIG_A: constants_a["LATENCY_FIRST_OUT"], CONFIG_B: constants_b["LATENCY_FIRST_OUT"]}
    model_a, model_b = stand_in(CONFIG_A, file_a), stand_in(CONFIG_B, file_b)
    assert model_a.name != model_b.name
    toplevel = HERE / ("xfft_multi.vhdl" if hdl_sources.toplevel_lang("engine", sim) == "vhdl" else "xfft_multi.sv")
    together = [[(prefix, name) for prefix, (_, name) in INSTANCES.items()]]
    each = [[(prefix, name)] for prefix, (_, name) in INSTANCES.items()]
    clean = [[(prefix, f"{name}_clean") for prefix, (_, name) in INSTANCES.items()]]
    multi = simulate(sim, "three", [model_a, model_b, toplevel], "xfft_multi",
                     {prefix: cfg for prefix, (cfg, _) in INSTANCES.items()}, together + each + clean)
    for prefix, (cfg, name) in INSTANCES.items():
        frame = multi[-1][f"{name}_clean"]["frames"][0]
        assert frame["first_out"] - frame["first_in"] == latency[cfg], \
            f"{sim}: {name}: a clean frame came out after {frame['first_out'] - frame['first_in']}, " \
            f"not {latency[cfg]}"
    multi = multi[:-1]
    only_a = simulate(sim, "only_a", [model_a], CONFIG_A.module, {"": CONFIG_A}, [[("", "a0")], [("", "a1")]])
    only_b = simulate(sim, "only_b", [model_b], CONFIG_B.module, {"": CONFIG_B}, [[("", "b")]])
    alone = {"a0": only_a[0]["a0"], "a1": only_a[1]["a1"], "b": only_b[0]["b"]}
    failures = []
    for name, want in alone.items():
        assert len(want["frames"]) == TRAFFIC[name]["frames"] and all(f["last_out"] > 0 for f in want["frames"])
        for label, got in (("all three at once", multi[0][name]),
                           ("alone in the shared simulation", next(p[name] for p in multi[1:] if name in p))):
            diff = difference(want, got)
            if diff:
                failures.append(f"{name}, {label}: {diff}")
    assert alone["a0"] != alone["b"], "the two configurations' results should differ"
    assert not failures, f"{sim}: instances differ from their configuration alone:\n  " + "\n  ".join(failures)
