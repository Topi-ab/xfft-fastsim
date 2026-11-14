"""The generated core (a cocotb-vivado VivadoIp) on Riviera-PRO: Vivado generates it and exports its simulation
sources for Riviera (export_simulation -simulator riviera); the design library (xil_defaultlib) is compiled with the
test's sources, every IP library comes precompiled from AMD's simulation libraries compiled for Riviera.

The libraries: compiled once with Vivado into a directory outside the repository, named by XFFT_RIVIERA_LIBS:

    vivado -mode tcl
    compile_simlib -simulator riviera -simulator_exec_path <Riviera's bin> -family all -language all \
        -library all -directory <dir>

<dir> has a subdirectory per library (<dir>/<library>/<library>.lib). The core's IP libraries are compiled there
from AMD's encrypted sources (IEEE 1735), which Riviera decrypts; none of them is compiled from the export. A
library the export needs that is not in <dir> is an error: it was compiled with another Vivado (the library names
carry the IP versions), compile again.

Used by sim_runner.build_riviera(); knows no DUT.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LIBS_ENV = "XFFT_RIVIERA_LIBS"
EXPORT_TCL = "export_riviera.tcl"
STAMP = "export_riviera.sha256"
# The VHDL libraries of the Verilog ones that export_simulation lists for asim: a VHDL design resolves its library
# clauses through the library mapping, not through -L
VHDL_COUNTERPART = {"unisims_ver": "unisim", "unimacro_ver": "unimacro"}


class RivieraIpError(Exception):
    """The core cannot be simulated on Riviera here (no libraries, a library missing, an export not understood)."""


@dataclass
class RivieraIp:
    """What a design with the generated core needs on Riviera.

    sources: the design library's files in compile order, (path, "vhdl" | "verilog", compiler options).
    libraries: the precompiled libraries to map, name -> its .lib file. asim_libraries: asim's -L list.
    tops: further toplevels for asim (glbl)."""
    sources: list = field(default_factory=list)
    libraries: dict = field(default_factory=dict)
    asim_libraries: list = field(default_factory=list)
    tops: list = field(default_factory=list)


def libs_dir() -> Path | None:
    """The compiled simulation libraries (XFFT_RIVIERA_LIBS), or None when not set."""
    value = os.environ.get(LIBS_ENV, "").strip()
    return Path(value).resolve() if value else None


def unavailable() -> str | None:
    """Why the generated core cannot run on Riviera here, or None."""
    libs = libs_dir()
    if libs is None:
        return (f"{LIBS_ENV} not set: AMD's simulation libraries compiled for Riviera (compile_simlib -simulator "
                f"riviera, docs/USER_GUIDE.md)")
    if not libs.is_dir():
        return f"{LIBS_ENV}={libs} is not a directory"
    return None


def library_file(libs: Path, name: str) -> Path | None:
    """The .lib of a precompiled library, or None."""
    lib = libs / name / f"{name}.lib"
    return lib if lib.is_file() else None


def export_tcl(ip, libs: Path) -> str:
    """Vivado batch script, run in the export directory: the builder Tcl generates the core into ./ip/ in an
    in-memory project, export_simulation writes its Riviera scripts into ./export/<module>/riviera/."""
    if ip.builder_tcl is None:
        raise RivieraIpError("the generated core on Riviera needs a VivadoIp with a builder Tcl")
    lines = [f"source {{{Path(ip.builder_tcl).resolve()}}}"]
    for xci in ip.paths:
        lines.append(f"export_simulation -of_objects [get_files [file normalize {{{xci}}}]] -simulator riviera "
                     f"-directory ./export -lib_map_path {{{libs}}} -use_ip_compiled_libs -force")
    return "\n".join(lines) + "\n"


def export(ip, out: Path) -> list[Path]:
    """Generate and export the core into out unless the stamp says it is there; the export's riviera/ directory of
    each IP. Vivado's log in out/vivado.log."""
    libs = libs_dir()
    reason = unavailable()
    if reason:
        raise RivieraIpError(reason)
    out.mkdir(parents=True, exist_ok=True)
    tcl = export_tcl(ip, libs)
    digest = hashlib.sha256((tcl + Path(ip.builder_tcl).read_text()).encode()).hexdigest()
    dirs = [out / "export" / Path(xci).stem / "riviera" for xci in ip.paths]
    stamp = out / STAMP
    if stamp.is_file() and stamp.read_text() == digest and all((d / "compile.do").is_file() for d in dirs):
        return dirs
    stamp.unlink(missing_ok=True)
    (out / EXPORT_TCL).write_text(tcl)
    result = subprocess.run(["vivado", "-mode", "batch", "-nojournal", "-log", "vivado.log", "-source", EXPORT_TCL],
                            cwd=out, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RivieraIpError(f"vivado failed ({result.returncode}) exporting the core for Riviera, see "
                             f"{out / 'vivado.log'}")
    missing = [d for d in dirs if not (d / "compile.do").is_file()]
    if missing:
        raise RivieraIpError(f"export_simulation wrote no {missing[0] / 'compile.do'}, see {out / 'vivado.log'}")
    stamp.write_text(digest)
    return dirs


# A compile command of compile.do and its files: `vcom -work <lib> <options> \` and one `"<file>"` per line, all but
# the last followed by ` \`
COMPILE = re.compile(r'^(vlog|vcom) -work (\S+)([^\n]*?)\s*\\\n((?:"[^"\n]+"(?: \\)?\n?)+)', re.M)
# The options of the design library's compile commands that are passed on (the language standard); -incr is
# dropped (a build from scratch), any other is an error: an export this module does not know
STANDARDS = {"vcom": ("-93", "-2002", "-2008", "-2019"), "vlog": ("-sv", "-v2k5", "-v2k")}


def parse(riviera_dir: Path, design_library: str, libs: Path) -> RivieraIp:
    """The export of one IP (its riviera/ directory): the design library's files, the precompiled libraries the
    rest needs, asim's -L list and toplevels."""
    compile_do = (riviera_dir / "compile.do").read_text()
    simulate_do = (riviera_dir / "simulate.do").read_text()
    out = RivieraIp()
    needed = list(dict.fromkeys(re.findall(r"^vlib riviera/(\S+)", compile_do, re.M)))
    for tool, lib, options, files in COMPILE.findall(compile_do):
        if lib != design_library:
            continue
        opts = [o for o in options.split() if o != "-incr"]
        unknown = [o for o in opts if o not in STANDARDS[tool]]
        if unknown:
            raise RivieraIpError(f"{riviera_dir / 'compile.do'}: {tool} -work {lib} with options {unknown}, not "
                                 f"known here")
        for f in re.findall(r'"([^"\n]+)"', files):
            path = (riviera_dir / f).resolve()
            if not path.is_file():
                raise RivieraIpError(f"{riviera_dir / 'compile.do'}: {f} not found")
            out.sources.append((path, "vhdl" if tool == "vcom" else "verilog", opts))
    if not out.sources:
        raise RivieraIpError(f"{riviera_dir / 'compile.do'}: nothing compiled into {design_library}")
    asim = next((line for line in simulate_do.splitlines() if line.startswith("asim ")), None)
    if asim is None:
        raise RivieraIpError(f"{riviera_dir / 'simulate.do'}: no asim command")
    words = asim.split()
    out.asim_libraries = [words[i + 1] for i, w in enumerate(words[:-1]) if w == "-L"]
    out.tops = [w for w in words if w.startswith(f"{design_library}.") and w.endswith(".glbl")]
    compiled = {p.stem for p, _, _ in out.sources}
    for top in out.tops:
        if top.split(".", 1)[1] not in compiled:
            raise RivieraIpError(f"{riviera_dir / 'simulate.do'}: toplevel {top}, but compile.do compiles no "
                                 f"{top.split('.', 1)[1]} into {design_library}")
    for name in [*needed, *out.asim_libraries]:
        for lib in (name, VHDL_COUNTERPART.get(name)):
            if lib is None or lib in (design_library, "work") or lib in out.libraries:
                continue
            found = library_file(libs, lib)
            if found is None:
                if lib == VHDL_COUNTERPART.get(name):
                    continue
                raise RivieraIpError(f"{lib} is not in {LIBS_ENV}={libs}: compile AMD's simulation libraries for "
                                     f"Riviera with this Vivado (compile_simlib -simulator riviera)")
            out.libraries[lib] = found
    return out


def merge(parts: list[RivieraIp]) -> RivieraIp:
    """The exports of several IPs as one."""
    out = RivieraIp()
    for p in parts:
        out.sources += [s for s in p.sources if s[0] not in {x[0] for x in out.sources}]
        out.libraries |= p.libraries
        out.asim_libraries += [lib for lib in p.asim_libraries if lib not in out.asim_libraries]
        out.tops += [t for t in p.tops if t not in out.tops]
    return out


def map_script(libraries: dict) -> str:
    """vsimsa script that maps the precompiled libraries in the current directory's library.cfg."""
    return "\n".join([*(f"amap {name} {{{path}}}" for name, path in libraries.items()), "exit", ""])
