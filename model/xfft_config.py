"""Configuration of the xfft core: the one place that knows its parameters.

Everything follows from it: the IP (ip/xfft_core.tcl, through the builder Tcl written here), the C model's
generics (passed to xfft_dpi_new at run time), the port widths of the stand-in, the test benches and the build
directories. The timing constants are per configuration (model/xfft_timing.py).

The default is a small core (1024 points), so that runs of the real core take minutes. Other configurations are
chosen with XFFT_CONFIG, a comma-separated list of overrides, e.g. the 16k core with 24-bit data:

    XFFT_CONFIG=nfft=14,input_width=24,twiddle_width=18 pytest tests/

A configuration other than those that ship with constants needs its timing constants measured first
(docs/CHARACTERIZATION.md).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IP_TCL = REPO / "ip" / "xfft_core.tcl"
BUILD = REPO / "build"

ROUNDINGS = ("convergent", "truncation")
SCALINGS = ("scaled",)
DEFAULT_MODULE = "xfft_core"
# Modules whose package, C prefix or wrapper file would be those of the default module (xfft_model_pkg,
# xfft_engine, model/xfft_engine.sv/.vhdl); refused in any case, as VHDL names do not depend on case
RESERVED_MODULES = ("xfft", "xfft_model", "xfft_engine")


def pad8(bits: int) -> int:
    """AXI4-Stream fields are padded to whole bytes."""
    return (bits + 7) // 8 * 8


@dataclass(frozen=True)
class XfftConfig:
    nfft: int = 10                  # log2 of the transform length
    input_width: int = 16           # data bits per component (output too: scaled)
    twiddle_width: int = 16         # phase factor bits
    scaling: str = "scaled"
    rounding: str = "convergent"    # convergent | truncation
    part: str = "xc7a35tcpg236-1"
    clock_mhz: int = 100            # target clock of the IP: may change its pipeline, so its latency
    module: str = DEFAULT_MODULE    # module name of the generated core, and of the stand-in

    def __post_init__(self) -> None:
        if not 3 <= self.nfft <= 16:
            raise ValueError(f"nfft={self.nfft}: 3 .. 16")
        if not 8 <= self.input_width <= 34 or not 8 <= self.twiddle_width <= 34:
            raise ValueError("input_width and twiddle_width: 8 .. 34")
        if self.scaling not in SCALINGS:
            raise ValueError(f"scaling={self.scaling}: the stand-in supports {', '.join(SCALINGS)} only")
        if self.rounding not in ROUNDINGS:
            raise ValueError(f"rounding={self.rounding}: one of {', '.join(ROUNDINGS)}")
        if not re.fullmatch(r"[A-Za-z](_?[A-Za-z0-9])*", self.module):
            raise ValueError(f"module={self.module}: a VHDL basic identifier, also valid in SV and C (a letter, then "
                             f"letters and digits, single underscores between them)")
        if self.module != DEFAULT_MODULE and self.module.lower() in (*RESERVED_MODULES, DEFAULT_MODULE):
            raise ValueError(f"module={self.module}: its names would be those of module {DEFAULT_MODULE} (VHDL "
                             f"names do not depend on case)")

    # ---- Derived numbers (PG109, pipelined streaming I/O, fixed scaling) ----

    @property
    def points(self) -> int:
        return 1 << self.nfft

    @property
    def stages(self) -> int:
        """Radix-4 stages (the last one radix-2 when nfft is odd): one SCALE_SCH field each."""
        return (self.nfft + 1) // 2

    @property
    def config_width(self) -> int:
        """s_axis_config_tdata: FWD_INV in bit 0, SCALE_SCH (2 bits per stage) above it."""
        return pad8(1 + 2 * self.stages)

    @property
    def data_width(self) -> int:
        """s_axis_data_tdata / m_axis_data_tdata: im in the upper half, re in the lower, each byte padded."""
        return 2 * pad8(self.input_width)

    @property
    def ovflo_bit(self) -> int:
        """m_axis_data_tuser: XK_INDEX in the low bytes, OVFLO in the byte above."""
        return pad8(self.nfft)

    @property
    def user_width(self) -> int:
        return pad8(self.nfft) + 8

    def max_scale(self, stage: int) -> int:
        """Largest SCALE_SCH value of a stage: the radix-2 last stage of an odd nfft takes 0 or 1 only."""
        return 1 if self.nfft % 2 and stage == self.stages - 1 else 3

    def config_word(self, forward: bool, schedule: list[int]) -> int:
        word = int(forward)
        for s, v in enumerate(schedule):
            assert 0 <= v <= self.max_scale(s)
            word |= v << (1 + 2 * s)
        return word

    # ---- Names ----

    @property
    def tag(self) -> str:
        """Short name of the configuration, for build directories and the timing table."""
        return (f"n{self.points}_d{self.input_width}_t{self.twiddle_width}_{self.rounding[:4]}_"
                f"{self.part}_{self.clock_mhz}mhz" + ("" if self.module == "xfft_core" else f"_{self.module}"))

    @property
    def spec(self) -> str:
        """XFFT_CONFIG value of this configuration (the fields that differ from the default)."""
        default = XfftConfig()
        return ",".join(f"{f.name}={getattr(self, f.name)}" for f in dataclasses.fields(self)
                        if getattr(self, f.name) != getattr(default, f.name))

    @property
    def engine_prefix(self) -> str:
        """Prefix of the C functions the stand-in's library exports (<prefix>_new, _step, _free, _wallclock) and
        the library's name (lib<prefix>.so): xfft_engine for module xfft_core, else <module>_engine. So the
        libraries of different configurations can share one simulation."""
        return "xfft_engine" if self.module == DEFAULT_MODULE else f"{self.module}_engine"

    @property
    def model_package(self) -> str:
        """Name of the generated HDL package (SV and VHDL) of the stand-in's widths and constants, and of its
        files: xfft_model_pkg for module xfft_core, else <module>_pkg."""
        return "xfft_model_pkg" if self.module == DEFAULT_MODULE else f"{self.module}_pkg"

    @property
    def build_dir(self) -> Path:
        return BUILD / self.tag

    def ip_params(self) -> str:
        """The parameter dict of create_xfft_core (ip/xfft_core.tcl)."""
        rounding = {"convergent": "convergent_rounding", "truncation": "truncation"}[self.rounding]
        return (f"points {self.points} input_width {self.input_width} twiddle_width {self.twiddle_width} "
                f"scaling {self.scaling} rounding {rounding} clock_mhz {self.clock_mhz}")

    def builder_tcl(self) -> str:
        """Vivado batch script that generates the core in ./ip/ (the working directory): its simulation files and
        its C model (the IP's cmodel output product, so the C model always matches the core's version).

        Self-contained (ip/xfft_core.tcl inlined), so that its contents are everything the generation depends
        on: cocotb-vivado and the result cache hash it, and it holds no machine paths."""
        return "\n".join([
            f"# Generated by model/xfft_config.py for {self.tag}; do not edit.",
            IP_TCL.read_text(),
            f"create_project -in_memory -part {self.part}",
            "set_property target_language VHDL [current_project]",
            "set_property simulator_language Mixed [current_project]",
            "file delete -force [file join [pwd] ip]",
            f"set ip [create_xfft_core [file join [pwd] ip] {self.module} {{{self.ip_params()}}}]",
            "generate_target {instantiation_template simulation} $ip",
            f"set targets [list_targets [get_files {self.module}.xci]]",
            "if {[lsearch -exact $targets cmodel] < 0} {",
            "    error \"the xfft IP of this Vivado has no cmodel output product (list_targets: $targets)\"",
            "}",
            "generate_target cmodel $ip",
            "",
        ])

    def write_builder_tcl(self) -> Path:
        """The builder Tcl in the build directory (rewritten only when it changes, so its mtime stays)."""
        path = self.build_dir / "build_ip.tcl"
        text = self.builder_tcl()
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return path

    @property
    def xci(self) -> str:
        """The .xci the builder Tcl writes, relative to its working directory."""
        return f"ip/{self.module}/{self.module}.xci"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.builder_tcl().encode()).hexdigest()


def parse(spec: str) -> XfftConfig:
    """XfftConfig from an XFFT_CONFIG value ("nfft=14,input_width=24")."""
    fields = {f.name: f.type for f in dataclasses.fields(XfftConfig)}
    values = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or name not in fields:
            raise ValueError(f"XFFT_CONFIG: {item!r}; expected name=value with name one of {', '.join(fields)}")
        values[name] = int(value) if fields[name] == "int" else value.strip()
    return XfftConfig(**values)


def current() -> XfftConfig:
    """The configuration of this run: the default with the overrides of XFFT_CONFIG."""
    return parse(os.environ.get("XFFT_CONFIG", ""))
