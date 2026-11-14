"""The configuration of an existing xfft IP (.xci), read with Vivado, as the bench's configuration.

    report = read_config("my_ip/xfft_0/xfft_0.xci")     # Vivado in batch mode (scripts/read_xci.tcl)
    report.config                                        # an XfftConfig
    report.unsupported, report.differences

Vivado opens the .xci (read_ip in an in-memory project; nothing is generated or written next to it) and gives
every CONFIG.* property, the IP definition and the part. They map to model/xfft_config.XfftConfig:

    transform_length            nfft (log2; a power of two)
    input_width                 input_width
    phase_factor_width          twiddle_width
    rounding_modes              rounding (convergent_rounding -> convergent, truncation)
    target_clock_frequency      clock_mhz (whole MHz)
    Component_Name              module
    PART                        part

A Versal part (Vivado's ARCHITECTURE or FAMILY of the part, characterize/parts.py) is refused before the bench's
core is generated.

The options that ip/xfft_core.tcl fixes (FIXED below: the architecture, the scaling, the output order, the
throttling, the tuser fields, the reset, ...) must have the values the stand-in is written for. An IP with
another value is unsupported: map_config() lists every such option, and characterize() refuses it.

All the other options (memory types, butterfly and multiplier types, the stages in block RAM, ...) are not
parameters of the bench: it generates its core from the mapped configuration with ip/xfft_core.tcl, and they
get Vivado's defaults there. differences() reads the generated core's .xci with Vivado too and lists every option
whose value differs from the user's IP, and the IP version when it is not the one this Vivado generates (the
bench always takes the newest xfft 9.x of the installation). Such a difference can change the core's latency, so
characterize() refuses a configuration with differences unless it is allowed (allow_differences=True, CLI
--allow-differences); the timing constants then belong to the generated core, not exactly to the user's IP.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import xfft_config

from . import parts

READER = xfft_config.REPO / "scripts" / "read_xci.tcl"
WORK = xfft_config.REPO / "build" / "characterize" / "xci"

# CONFIG option -> (the value the stand-in needs, what it is). ip/xfft_core.tcl sets or relies on these.
FIXED = {
    "implementation_options": ("pipelined_streaming_io", "architecture: pipelined streaming I/O only"),
    "data_format": ("fixed_point", "data format: fixed point only"),
    "scaling_options": ("scaled", "scaling: fixed scaling (scaled) only, SCALE_SCH on the config channel"),
    "output_ordering": ("natural_order", "output order: natural order only"),
    "throttle_scheme": ("nonrealtime", "throttling: non-real-time only (the core honours m_axis_data_tready)"),
    "xk_index": ("true", "XK_INDEX in m_axis_data_tuser"),
    "ovflo": ("true", "OVFLO in m_axis_data_tuser and m_axis_status"),
    "aresetn": ("true", "aresetn port"),
    "aclken": ("false", "no aclken port"),
    "run_time_configurable_transform_length": ("false", "run-time configurable transform length: not modelled"),
    "cyclic_prefix_insertion": ("false", "cyclic prefix insertion: not modelled"),
    "channels": ("1", "one channel only"),
    "super_sample_rates": ("1", "super sample rate: 1 only"),
}
# CONFIG options that map to XfftConfig fields (with PART and the IP definition)
MAPPED = ("transform_length", "input_width", "phase_factor_width", "rounding_modes", "target_clock_frequency",
          "Component_Name")
ROUNDINGS = {"convergent_rounding": "convergent", "truncation": "truncation"}


class XciError(Exception):
    """The .xci cannot be read, or holds a configuration the stand-in does not support."""


@dataclass(frozen=True)
class IpProperties:
    """What Vivado says about one IP instance."""
    xci: Path
    vlnv: str                       # IPDEF, e.g. xilinx.com:ip:xfft:9.1
    part: str
    config: dict[str, str]          # CONFIG.<name> -> value, the CONFIG. prefix dropped
    newest_xfft: str                # the newest xfft of this Vivado (what the bench generates)
    vivado: str                     # version -short
    architecture: str = ""          # Vivado's ARCHITECTURE of the part ("" when this Vivado does not know it)
    family: str = ""                # Vivado's FAMILY of the part

    @property
    def version(self) -> str:
        return self.vlnv.rsplit(":", 1)[-1]


@dataclass
class XciReport:
    """The configuration of an .xci: the mapped XfftConfig (None when unsupported), the unsupported options, and
    the options in which the core the bench generates differs from the IP."""
    xci: Path
    properties: IpProperties
    config: xfft_config.XfftConfig | None
    unsupported: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)


def read_properties(*xcis: Path, log=None) -> list[IpProperties]:
    """Open each .xci with Vivado (batch mode, scripts/read_xci.tcl) and read its properties. The .xci files are
    copied into a scratch directory first, so that Vivado writes nothing next to them."""
    if shutil.which("vivado") is None:
        raise XciError("vivado is not on PATH: source <Vivado>/settings64.sh")
    WORK.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        copies = []
        for i, xci in enumerate(xcis):
            xci = Path(xci)
            if not xci.is_file():
                raise XciError(f"{xci}: no such file")
            copy = tmp / str(i) / xci.name
            copy.parent.mkdir()
            shutil.copy2(xci, copy)
            copies.append(copy)
        out = tmp / "properties.tsv"
        if log:
            log(f"characterize: reading {', '.join(str(x) for x in xcis)} with Vivado")
        result = subprocess.run(["vivado", "-mode", "batch", "-nojournal", "-log", str(tmp / "vivado.log"),
                                 "-source", str(READER), "-tclargs", str(out), *map(str, copies)],
                                cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0 or not out.is_file():
            errors = [line for line in result.stdout.splitlines() if "ERROR" in line] or \
                result.stdout.splitlines()[-15:]
            raise XciError("Vivado could not read the .xci:\n" + "\n".join(errors))
        return parse_properties(out.read_text(), list(map(Path, xcis)))


def parse_properties(text: str, xcis: list[Path]) -> list[IpProperties]:
    """The IpProperties of the reader's output (scripts/read_xci.tcl), in the order of xcis."""
    vivado, blocks = "", []
    for line in text.splitlines():
        key, _, value = line.partition("\t")
        if key == "vivado":
            vivado = value
        elif key == "ip":
            blocks.append({"CONFIG": {}})
        elif key.startswith("CONFIG."):
            blocks[-1]["CONFIG"][key[len("CONFIG."):]] = value
        elif blocks:
            blocks[-1][key] = value
    if len(blocks) != len(xcis):
        raise XciError(f"the reader gave {len(blocks)} IPs for {len(xcis)} .xci files")
    return [IpProperties(xci=x, vlnv=b.get("IPDEF", ""), part=b.get("PART", ""), config=b["CONFIG"],
                         newest_xfft=b.get("newest_xfft", ""), vivado=vivado, architecture=b.get("ARCHITECTURE", ""),
                         family=b.get("FAMILY", "")) for x, b in zip(xcis, blocks)]


def map_config(props: IpProperties) -> tuple[xfft_config.XfftConfig | None, list[str]]:
    """(the XfftConfig of the IP, the unsupported options); the config is None when anything is unsupported."""
    c = {k.lower(): v for k, v in props.config.items()}
    unsupported = []
    m = re.fullmatch(r"xilinx\.com:ip:xfft:(\d+)\.(\d+)", props.vlnv)
    if not m:
        unsupported.append(f"IP definition {props.vlnv or '(none)'}: not an xfft core")
    elif m[1] != "9":
        unsupported.append(f"IP definition {props.vlnv}: xfft {m[1]}.{m[2]}; the stand-in is written for xfft 9.x")
    for name, (needed, what) in FIXED.items():
        value = c.get(name.lower())
        if value is None:
            unsupported.append(f"CONFIG.{name} missing ({what})")
        elif value.lower() != needed:
            unsupported.append(f"CONFIG.{name} = {value}: {what} ({needed})")
    values = {}
    try:
        points = int(c.get("transform_length", ""))
        if points < 1 or points & (points - 1):
            raise ValueError
        values["nfft"] = points.bit_length() - 1
    except ValueError:
        unsupported.append(f"CONFIG.transform_length = {c.get('transform_length')}: not a power of two")
    for name, key in (("input_width", "input_width"), ("phase_factor_width", "twiddle_width")):
        try:
            values[key] = int(c.get(name, ""))
        except ValueError:
            unsupported.append(f"CONFIG.{name} = {c.get(name)}: not an integer")
    rounding = c.get("rounding_modes", "")
    if rounding in ROUNDINGS:
        values["rounding"] = ROUNDINGS[rounding]
    else:
        unsupported.append(f"CONFIG.rounding_modes = {rounding}: one of {', '.join(ROUNDINGS)}")
    clock = c.get("target_clock_frequency", "")
    try:
        mhz = float(clock)
        if mhz != int(mhz) or mhz <= 0:
            raise ValueError
        values["clock_mhz"] = int(mhz)
    except ValueError:
        unsupported.append(f"CONFIG.target_clock_frequency = {clock}: the bench takes whole MHz only")
    if not props.part:
        unsupported.append("PART: the IP names no part")
    values["part"] = props.part
    values["module"] = props.config.get("Component_Name") or props.config.get("component_name") or "xfft_core"
    if unsupported:
        return None, unsupported
    try:
        return xfft_config.XfftConfig(**values), []
    except ValueError as e:
        return None, [str(e)]


def differences(user: IpProperties, generated: IpProperties) -> list[str]:
    """The options in which the core the bench generates differs from the user's IP: every CONFIG option of
    either (interface parameters such as CONFIG.S_AXIS_DATA.INSERT_VIP and the mapped ones aside, which agree by
    construction), and the IP version."""
    out = []
    if user.vlnv != generated.vlnv:
        out.append(f"IP version: the .xci is {user.vlnv}, this Vivado generates {generated.vlnv} (the bench takes "
                   f"the newest xfft 9.x it has)")
    a = {k.lower(): v for k, v in user.config.items() if "." not in k}
    b = {k.lower(): v for k, v in generated.config.items() if "." not in k}
    skip = {k.lower() for k in MAPPED}
    for name in sorted((set(a) | set(b)) - skip):
        if a.get(name, "").lower() != b.get(name, "").lower():
            out.append(f"CONFIG.{name}: the .xci has {a.get(name, '(none)')}, the generated core "
                       f"{b.get(name, '(none)')}")
    return out


def read_config(xci: Path, log=None) -> XciReport:
    """Read the .xci with Vivado and map it (map_config()); for a supported IP, also generate the bench's core of
    that configuration (scripts/generate_ip.py) and list where it differs from the IP (differences()). XciError
    for a Versal part (characterize/parts.py), before anything is generated."""
    import generate_ip

    user, = read_properties(Path(xci), log=log)
    if parts.is_versal(user.part, user.architecture, user.family):
        raise XciError(f"{xci}: {parts.versal_refusal(user.part)}")
    cfg, unsupported = map_config(user)
    report = XciReport(Path(xci), user, cfg, unsupported)
    if cfg is None:
        return report
    if log:
        log(f"characterize: {xci}: {user.vlnv}, configuration {cfg.tag} (XFFT_CONFIG={cfg.spec})")
    try:
        ip_dir = generate_ip.generate(cfg)
    except SystemExit as e:             # generate_ip reports a failed Vivado run so
        raise XciError(str(e)) from None
    generated, = read_properties(ip_dir / f"{cfg.module}.xci", log=log)
    report.differences = differences(user, generated)
    return report


def config_from_xci(xci: str | os.PathLike, allow_differences: bool = False, log=None) -> xfft_config.XfftConfig:
    """The XfftConfig of an .xci, or XciError when it holds an unsupported option or (unless allowed) differs from
    the core the bench generates."""
    report = read_config(Path(xci), log=log)
    if report.unsupported:
        raise XciError(f"{xci}: options the stand-in does not support:\n  " + "\n  ".join(report.unsupported))
    if report.differences and not allow_differences:
        raise XciError(f"{xci}: the core the bench generates for {report.config.tag} differs from this IP in:\n  "
                       + "\n  ".join(report.differences)
                       + "\nThe constants would belong to the generated core; pass allow_differences=True "
                         "(--allow-differences) to characterize it anyway.")
    if report.differences and log:
        for d in report.differences:
            log(f"characterize: warning: {d}")
    return report.config
