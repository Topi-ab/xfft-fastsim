"""What the tests compile: the xfft core as FFT_MODEL selects it, for the configuration of XFFT_CONFIG.

FFT_MODEL:
    rtl     the generated core (ip/xfft_core.tcl), simulated through its encrypted model; the default. XSim, and
            Riviera with AMD's simulation libraries compiled for it (riviera_ip.py, XFFT_RIVIERA_LIBS)
    engine  the stand-in: the C engine (model/xfft_engine.c) with the core's timing and AMD's bit-accurate C model,
            behind the SV wrapper model/xfft_engine.sv (DPI-C) on XSim, Verilator and Riviera and the VHDL wrapper
            model/xfft_engine.vhdl (VHPIDIRECT) on NVC and GHDL. Its C model comes from the generated core, which
            needs Vivado once (scripts/generate_ip.py); after that NVC, GHDL, Verilator and Riviera need no Vivado
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import cmodel_archive
import generate_ip
import riviera_ip
import sim_runner
import xfft_config
import xfft_timing
from sim_runner import DpiModel

MODELS = ("rtl", "engine")
MODEL_DIR = xfft_config.REPO / "model"
SCRIPTS = xfft_config.REPO / "scripts"
SIMULATORS = ("vivado", "nvc", "ghdl", "verilator", "riviera")
# The simulators of the generated core (encrypted IP), and why it does not run on the others
ENCRYPTED_IP_SIMULATORS = ("vivado", "riviera")
ENCRYPTED_IP = "the generated core is encrypted IP: XSim and Riviera only"
# Why a model does not run on NVC, GHDL and Verilator
SIMULATOR_LIMITS = {"rtl": ENCRYPTED_IP}


def fft_model() -> str:
    """The xfft core of the tests: FFT_MODEL, rtl (default) or engine."""
    model = os.environ.get("FFT_MODEL") or "rtl"
    if model == "cmodel":
        raise ValueError("FFT_MODEL=cmodel is no longer supported: the stand-in is FFT_MODEL=engine (the C engine, "
                         "on XSim, NVC, GHDL, Verilator and Riviera)")
    if model not in MODELS:
        raise ValueError(f"FFT_MODEL={model}: expected one of {', '.join(MODELS)}")
    return model


def simulators(model: str | None = None, xsim_only: str | None = None, encrypted_ip: bool = False) -> list:
    """pytest parameters vivado, nvc, ghdl, verilator and riviera for a test of the model (default FFT_MODEL),
    each skipped where the simulator is not installed. A test that needs XSim whatever the model (xsim_only: the
    reason) runs on vivado only. A test of the generated core (model rtl, or encrypted_ip: it needs the core
    whatever the model) runs on vivado and riviera only, on riviera only with AMD's simulation libraries
    compiled for it (riviera_ip.py, XFFT_RIVIERA_LIBS)."""
    model = model or fft_model()
    needs_ip = encrypted_ip or model in SIMULATOR_LIMITS
    params = []
    for param in sim_runner.simulators(*SIMULATORS):
        sim = param.values[0]
        reason = None
        if xsim_only and sim != "vivado":
            reason = f"{xsim_only}: XSim only"
        elif needs_ip and sim not in ENCRYPTED_IP_SIMULATORS:
            reason = ENCRYPTED_IP
        elif needs_ip and sim == "riviera":
            reason = riviera_ip.unavailable()
        if reason:
            param = pytest.param(sim, marks=[*param.marks, pytest.mark.skip(reason=f"{sim}: {reason}")])
        params.append(param)
    return params


def xfft_ip(cfg: xfft_config.XfftConfig):
    """The generated core as a cocotb-vivado source; its builder Tcl generates it in the build directory."""
    from cocotb_vivado.vivado import VivadoIp

    return VivadoIp(cfg.xci, builder_tcl=cfg.write_builder_tcl(), part_num=cfg.part)


def engine_inputs(cfg: xfft_config.XfftConfig, timing_only: bool = False, constants: Path | None = None) -> tuple:
    """The files the stand-in is made of: its files in model/, the modules its build script imports, the C-model
    archive of the generated core (generate_ip.py runs Vivado if the core is not there yet) and the measured
    constants (constants: the file the build reads as XFFT_TIMING_JSON, else as model/xfft_timing.py finds them).
    timing_only: model/xfft_dpi_stub.c instead of xfft_dpi.c and the C-model archive (no Vivado)."""
    shim = "xfft_dpi_stub.c" if timing_only else "xfft_dpi.c"
    inputs = [MODEL_DIR / f for f in ("xfft_engine.c", "xfft_engine.h", shim, "xfft_engine.sv",
                                      "xfft_engine.vhdl", "xfft_config.py", "xfft_timing.py", "cmodel_archive.py")]
    # The modules the build script imports from scripts/ (those from model/ are above)
    inputs.append(SCRIPTS / "generate_ip.py")
    if not timing_only:
        try:
            inputs.append(cmodel_archive.find(generate_ip.generate(cfg)))
        except FileNotFoundError as e:
            raise RuntimeError(f"the C model of {cfg.tag} comes with the generated core, which needs Vivado once "
                               f"({e.filename} not found): run scripts/generate_ip.py where Vivado is "
                               f"installed") from e
    if constants is not None:
        if not Path(constants).is_file():
            raise FileNotFoundError(f"{cfg.tag}: constants {constants}: no such file")
        inputs.append(Path(constants))
    else:
        measured = xfft_timing.source_file(cfg)
        if measured is not None:
            inputs.append(measured)
    return tuple(inputs)


def xfft_engine(cfg: xfft_config.XfftConfig, timing_only: bool = False, constants: Path | None = None) -> DpiModel:
    """The stand-in: the SV wrapper (XSim, Verilator, Riviera) or the VHDL wrapper (NVC, GHDL) with the core's
    name and ports, the C engine and the C model in its library (scripts/build_engine.py). timing_only: the engine
    without the C model (scripts/build_engine.py --timing-only: no Vivado, every output sample 0).

    The model is built for cfg, whatever XFFT_CONFIG the process has: its build gets XFFT_CONFIG=cfg.spec, and
    XFFT_TIMING_JSON=constants (the timing constants of cfg, e.g. a file in timing/), else the XFFT_TIMING_JSON the
    process has now, else none (model/xfft_timing.py looks them up). Its names follow cfg.module
    (scripts/build_engine.py): models of configurations with different modules can share one simulation
    (sim_runner.run() with several of them among its sources)."""
    default = cfg.module == xfft_config.DEFAULT_MODULE
    constants = constants if constants is not None else xfft_timing.override_file()
    if constants is not None:
        constants = Path(constants).resolve()
    pkg = cfg.model_package
    return DpiModel(
        name=cfg.engine_prefix,
        build=SCRIPTS / "build_engine.py",
        sources=(f"{pkg}.sv", MODEL_DIR / "xfft_engine.sv" if default else f"{cfg.module}.sv"),
        vhdl_sources=(f"{pkg}.vhdl", MODEL_DIR / "xfft_engine.vhdl" if default else f"{cfg.module}.vhdl"),
        inputs=engine_inputs(cfg, timing_only, constants),
        build_args=("--timing-only",) if timing_only else (),
        env=(("XFFT_CONFIG", cfg.spec), ("XFFT_TIMING_JSON", "" if constants is None else str(constants))),
    )


def xfft_core(cfg: xfft_config.XfftConfig, model: str | None = None):
    """The core as FFT_MODEL (or model) selects it."""
    model = model or fft_model()
    if model == "engine":
        return xfft_engine(cfg)
    return xfft_ip(cfg)


def toplevel_lang(model: str | None = None, sim: str = "vivado") -> str:
    """HDL language of the core as a toplevel: the IP's simulation wrapper is VHDL, the C engine's wrapper SV on
    XSim, Verilator and Riviera and VHDL on NVC and GHDL."""
    model = model or fft_model()
    if model == "engine":
        return "vhdl" if sim in ("nvc", "ghdl") else "verilog"
    return "vhdl"
