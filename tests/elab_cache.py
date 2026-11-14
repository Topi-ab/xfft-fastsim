"""Elaboration cache: XSim snapshots in the result cache's store, keyed by everything that decides them.

A snapshot depends on the build inputs only: the HDL sources (contents, in compile order), the IP builder Tcl and
part, the stand-in's (the C engine's) build script and files and the C-model archive (sim_cache.source_entry()), the
toplevel, library, generics and defines, the DPI libraries, the simulator (xelab version, Vivado's version.dat,
patches), the resolved Vivado installation path, the machine architecture, the cocotb and cocotb-vivado
versions (they write the compile and xelab commands) and the compiler environment variables when set. The key is
the sha256 of that manifest (elab_inputs()). Not in it: the test module, case, seed, run environment, the
repository's or the build directory's path, the Python version (no snapshot file links libpython), the OS and
glibc (recorded in the entry for diagnosis; the load check catches a host the snapshot does not run on).

After a successful elaboration the files the snapshot needs at run time are stored under the key: the snapshot
directory xsim.dir/<snapshot>/ (without XSim's kernel log), the DPI-C library and the C model's shared libraries
in dpi/, the small files the tests read from there (dpi/*.h, dpi/*.txt), the generated .xci files (ip/*/*.xci)
and build.log. Never the compiled libraries, xsim.ini or the rest of the generated IP: a snapshot does not read
them. Before an elaboration the key is looked up: the local build directory first (its stamp elab.json, every
listed file verified), then the store. A stored entry is restored into a temporary directory beside the build
directory, checked by loading the snapshot as cocotb-vivado will (load_error()), and only then put in place.

Portability (docs/CACHE.md, "Snapshot portability"): xelab links the DPI library into the snapshot by the path
-sv_root gives it. The layout this cache relies on keeps the DPI directory inside the elaboration directory
(<build dir>/dpi), so xelab writes ./dpi/libxfft_engine.so, which resolves against the test directory, and
scripts/build_engine.py links the library with the rpath $ORIGIN/cmodel. A snapshot also has the Vivado
installation path in its rpath; that path is in the key. Before an entry is stored, the dynamic sections of its
shared libraries are read (link_problem()): an absolute DPI library path, or the build directory's path in any
DT_NEEDED, DT_RPATH or DT_RUNPATH, and the entry is not stored (it would load another checkout's library).

Store: the result cache's (sim_cache.open_store()) under elab/, or SIM_ELAB_CACHE_URL. Layout under elab/:
    <key>/manifest.json         inputs, files {name: sha256 of the uncompressed file}, meta; written last
    <key>/files/<sha256>.gz     the files, gzipped, named by the sha256 of their content (the manifest maps the
                                names relative to the build directory to them)
    index/<build id>.json       the latest key of a build, to explain a miss
Two builders that store one key at the same time each write their own files (build.log and the kernel differ
between builds), so the manifest that lands last names files that are all there. An entry that restore() found
damaged is overwritten by the next save(); a complete one is kept (refresh overwrites it).
Restore writes only the names stored_files() makes (storable()): nothing else lands in the test directory.
SIM_ELAB_CACHE: on (default: reuse, restore, store), off (elaborate as without it), refresh (elaborate from
scratch and overwrite the entry). The cache never fails a test: a damaged or unreachable entry is a miss, logged.

Security: an entry is native code that the test process loads. Whoever can write to the store can run code on
every machine that reads it; give write access only to trusted builders.

CLI (python tests/sim_cache.py elab ..., or python tests/elab_cache.py ...): list | show <key> | diff <key> <key>
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import sim_cache

# The layout the stored snapshots rely on: the DPI directory in the build directory (dpi/), the DPI library's
# rpath $ORIGIN/cmodel, and how an entry is stored (2: files by content). A change makes every earlier entry
# unusable, so it is part of the key.
LAYOUT = 2
DPI_DIR = "dpi"
STAMP = "elab.json"
# Files XSim writes into the snapshot directory while a simulation runs
VOLATILE = {"xsimkernel.log"}
# Environment variables that change what the bundled compiler (xelab's C step) produces; xelab warns about
# them (XSIM 43-3431)
COMPILER_ENV = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "GCC_EXEC_PREFIX",
                "COMPILER_PATH")
# Packages that write the build commands
BUILD_DISTS = ("cocotb", "cocotb-vivado")
LOAD_TIMEOUT_S = 60
MODES = ("on", "off", "refresh")


def log(msg: str) -> None:
    print(f"elab_cache: {msg}", flush=True)


def mode() -> str:
    m = os.environ.get("SIM_ELAB_CACHE", "on").lower()
    if m not in MODES:
        raise sim_cache.CacheError(f"SIM_ELAB_CACHE={m}: expected one of {', '.join(MODES)}")
    return m


# ---- The key ----

def dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def elab_inputs(*, sources: Sequence, hdl_toplevel: str, hdl_library: str, parameters: Mapping | None = None,
                verilog_defines: Mapping | None = None) -> dict:
    """Everything that decides the snapshot (the module docstring), as a canonical JSON value.

    sources: as sim_runner.run() takes them (paths, VivadoIp, DpiModel), before the DPI models are built."""
    vivado = os.environ.get("XILINX_VIVADO")
    return json.loads(sim_cache.canonical({
        "layout": LAYOUT,
        "sources": [sim_cache.source_entry(s) for s in sources],
        "hdl_toplevel": hdl_toplevel,
        "hdl_library": hdl_library,
        "parameters": {k: str(v) for k, v in (parameters or {}).items()},
        "verilog_defines": {k: str(v) for k, v in (verilog_defines or {}).items()},
        "dpi_libraries": [f"lib{s.name}" for s in sources if hasattr(s, "cache_entry")],
        "tools": sim_cache.tool_fingerprint("vivado"),
        # The snapshot's rpath names the installation's bundled libraries by their absolute path
        "vivado_root": Path(vivado).resolve().as_posix() if vivado else None,
        "machine": platform.machine(),
        "packages": {d: dist_version(d) for d in BUILD_DISTS},
        "compiler_env": {k: os.environ[k] for k in COMPILER_ENV if k in os.environ},
    }))


# ---- What a snapshot is made of ----

def is_shared_library(name: str) -> bool:
    return name.endswith(".so") or ".so." in name


def runtime_files(build_dir: Path, snapshot: str) -> list[str]:
    """What a run of the snapshot reads, relative to build_dir: xsim.dir/<snapshot>/ (without the kernel log)
    and, in dpi/, the shared libraries and the small files the tests read (*.h and *.txt outside the C model)."""
    build_dir = Path(build_dir)
    out = [p for p in (build_dir / "xsim.dir" / snapshot).rglob("*") if p.is_file() and p.name not in VOLATILE]
    for p in (build_dir / DPI_DIR).rglob("*"):
        rel = p.relative_to(build_dir / DPI_DIR)
        if p.is_file() and (is_shared_library(p.name)
                            or (p.suffix in (".h", ".txt") and "cmodel" not in rel.parts[:-1])):
            out.append(p)
    return sorted(p.relative_to(build_dir).as_posix() for p in out)


def stored_files(build_dir: Path, snapshot: str) -> list[str]:
    """runtime_files() plus the generated .xci files (the core's version) and the build log (provenance)."""
    build_dir = Path(build_dir)
    extra = [p.relative_to(build_dir).as_posix() for p in build_dir.glob("ip/*/*.xci") if p.is_file()]
    if (build_dir / "build.log").is_file():
        extra.append("build.log")
    return sorted(set(runtime_files(build_dir, snapshot)) | set(extra))


def storable(name: str, snapshot: str) -> bool:
    """Whether name is one stored_files() can make: xsim.dir/<snapshot>/..., dpi/ shared libraries, headers and
    text files, ip/<core>/<core>.xci, build.log. restore() writes nothing else into the test directory (a stored
    cocotb.py or .pth there would shadow Python imports)."""
    if not sim_cache.safe_name(name):
        return False
    parts = name.split("/")
    if name == "build.log" or (parts[:2] == ["xsim.dir", snapshot] and len(parts) > 2
                               and parts[-1] not in VOLATILE):
        return True
    if parts[0] == DPI_DIR and len(parts) > 1:
        return is_shared_library(parts[-1]) or (parts[-1].endswith((".h", ".txt")) and "cmodel" not in parts[1:-1])
    return len(parts) == 3 and parts[0] == "ip" and parts[2].endswith(".xci")


def kernel(snapshot: str) -> str:
    return f"xsim.dir/{snapshot}/xsimk.so"


# ---- What a snapshot links: the dynamic section of an ELF file ----

DT_NEEDED, DT_STRTAB, DT_RPATH, DT_RUNPATH = 1, 5, 15, 29
PT_LOAD, PT_DYNAMIC = 1, 2


def dynamic_entries(path: Path) -> dict[str, list[str]] | None:
    """DT_NEEDED, DT_RPATH and DT_RUNPATH of a 64-bit ELF file, as the loader reads them (through the program
    headers), {"needed": [...], "rpath": [...], "runpath": [...]}; None when path is no such file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:4] != b"\x7fELF" or data[4:5] != b"\x02" or data[5:6] not in (b"\x01", b"\x02"):
        return None
    end = "<" if data[5:6] == b"\x01" else ">"
    try:
        phoff, = struct.unpack_from(end + "Q", data, 32)
        phentsize, phnum = struct.unpack_from(end + "HH", data, 54)
        loads, dynamic = [], None
        for i in range(phnum):
            p_type, _, p_offset, p_vaddr, _, p_filesz = struct.unpack_from(end + "IIQQQQ", data, phoff + i * phentsize)
            if p_type == PT_LOAD:
                loads.append((p_vaddr, p_offset, p_filesz))
            elif p_type == PT_DYNAMIC:
                dynamic = (p_offset, p_filesz)
        if dynamic is None:
            return None
        tags = []
        for off in range(dynamic[0], dynamic[0] + dynamic[1] - 15, 16):
            tag, val = struct.unpack_from(end + "qQ", data, off)
            if tag == 0:
                break
            tags.append((tag, val))
        strtab = next(v for t, v in tags if t == DT_STRTAB)
        base = next(o + strtab - a for a, o, n in loads if a <= strtab < a + n)
    except (struct.error, StopIteration):
        return None

    def string(offset: int) -> str:
        stop = data.index(b"\0", base + offset)
        return data[base + offset:stop].decode(errors="replace")

    try:
        out = {"needed": [], "rpath": [], "runpath": []}
        for tag, val in tags:
            kind = {DT_NEEDED: "needed", DT_RPATH: "rpath", DT_RUNPATH: "runpath"}.get(tag)
            if kind is not None:
                out[kind].append(string(val))
        return out
    except ValueError:
        return None


def link_problem(build_dir: Path, snapshot: str, dpi_libraries: Sequence[str]) -> str | None:
    """Why the snapshot in build_dir must not be stored, else None: its kernel is no ELF file with a dynamic
    section, it does not link a DPI library by a relative path, or a shared library among the stored files names
    the build directory (by its path as given or resolved) in DT_NEEDED, DT_RPATH or DT_RUNPATH. Such a snapshot
    loads the DPI library of the checkout it was built in, wherever it is restored."""
    build_dir = Path(build_dir)
    own = {build_dir.absolute().as_posix(), build_dir.resolve().as_posix()}
    top = dynamic_entries(build_dir / kernel(snapshot))
    if top is None:
        return f"{kernel(snapshot)} has no readable dynamic section"
    for lib in dpi_libraries:
        linked = [n for n in top["needed"] if n.rsplit("/", 1)[-1] == f"{lib}.so"]
        if not linked:
            return f"{kernel(snapshot)} does not link {lib}.so (needed: {', '.join(top['needed'])})"
        absolute = [n for n in linked if n.startswith("/")]
        if absolute:
            return f"{kernel(snapshot)} links the DPI library by an absolute path ({absolute[0]})"
    for name in stored_files(build_dir, snapshot):
        if not is_shared_library(name):
            continue
        entries = top if name == kernel(snapshot) else dynamic_entries(build_dir / name)
        for kind, values in (entries or {}).items():
            for v in values:
                if any(re.search(re.escape(d) + r"(/|$|:)", v) for d in own):
                    return f"{name} names the build directory in its {kind.upper()} ({v})"
    return None


def load_error(run_dir: Path, snapshot: str) -> str | None:
    """Load the snapshot's kernel library as cocotb-vivado does (from the test directory, with its
    LD_LIBRARY_PATH) in a subprocess: None when it loads, else the reason (the last line of the error). Catches
    what the key does not hold: a missing or older system library, another C++ runtime first on the path."""
    env = dict(os.environ)
    if "LD_LIBRARY_PATH" not in env and env.get("XILINX_VIVADO"):
        root = env["XILINX_VIVADO"]
        env["LD_LIBRARY_PATH"] = f"{root}/lib/lnx64.o:{root}/lib/lnx64.o/Default"     # cocotb-vivado's fallback
    try:
        r = subprocess.run([sys.executable, "-c", "import ctypes, sys; ctypes.CDLL(sys.argv[1])", kernel(snapshot)],
                           cwd=run_dir, env=env, capture_output=True, text=True, timeout=LOAD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"loading took more than {LOAD_TIMEOUT_S} s"
    except OSError as e:
        return str(e)
    if r.returncode == 0:
        return None
    lines = [line for line in r.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit code {r.returncode}"


def hashes(build_dir: Path, names: Sequence[str]) -> dict[str, str]:
    return {n: sim_cache.sha256_file(Path(build_dir) / n) for n in names}


def size(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 100_000 else f"{n / 1e3:.0f} kB"


# ---- Store ----

def sub_store(store, name: str):
    """The store's directory or prefix <name>/: elaboration entries live apart from the results (the result
    cache's listing only sees <key>/manifest.json at the top)."""
    if isinstance(store, sim_cache.DirStore):
        return sim_cache.DirStore(store.root / name)
    sub = copy.copy(store)
    sub.prefix = store.prefix + name + "/"
    return sub


def open_store(url: str | None = None):
    """SIM_ELAB_CACHE_URL, else the result cache's store (SIM_CACHE_URL, else .sim_cache), under elab/."""
    return sub_store(sim_cache.open_store(url or os.environ.get("SIM_ELAB_CACHE_URL") or None), "elab")


def host() -> dict:
    """The elaborating host, for diagnosis only (not in the key)."""
    libc, version = platform.libc_ver()
    return {"os": platform.platform(), "libc": f"{libc} {version}".strip(), "python": platform.python_version()}


class CachedElab:
    """One elaboration: its key, the local check, the restore from and the save to the store.

    build_id names the build in the store's index (to explain a miss): the build directory below the build
    root, for example "n256_d16_t16_conv_xc7a35tcpg236-1_100mhz/xfft_flow/vivado"."""

    def __init__(self, build_id: str, inputs: Mapping, store=None) -> None:
        self.build_id = build_id
        self.inputs = json.loads(sim_cache.canonical(inputs))
        self.key = sim_cache.make_key(self.inputs)
        self.store = store if store is not None else open_store()
        # Set by restore() when the entry is there but damaged: the next save() replaces it
        self.damaged = False

    def local(self, build_dir: Path, snapshot: str) -> bool:
        """True when build_dir holds this key's snapshot: its stamp has the key and every listed file verifies."""
        try:
            stamp = json.loads((Path(build_dir) / STAMP).read_text())
            files = stamp["files"]
            if stamp.get("key") != self.key or kernel(snapshot) not in files:
                return False
            bad = [n for n, h in files.items()
                   if not sim_cache.safe_name(n) or not (Path(build_dir) / n).is_file()
                   or sim_cache.sha256_file(Path(build_dir) / n) != h]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        if bad:
            log(f"{self.build_id}: local snapshot {self.key[:16]} does not verify ({', '.join(bad[:3])})")
            return False
        log(f"{self.build_id}: local snapshot {self.key[:16]} verified ({len(files)} files); not elaborated")
        return True

    def write_stamp(self, build_dir: Path, snapshot: str, origin: str) -> dict[str, str]:
        """After a build or a restore: elab.json with the key and the stored files' hashes, written last."""
        files = hashes(build_dir, stored_files(build_dir, snapshot))
        if kernel(snapshot) not in files:
            raise sim_cache.CacheError(f"no {kernel(snapshot)} in {build_dir}")
        stamp = {"key": self.key, "build_id": self.build_id, "origin": origin, "files": files}
        (Path(build_dir) / STAMP).write_text(json.dumps(stamp, indent=1, sort_keys=True) + "\n")
        return files

    def restore(self, build_dir: Path, snapshot: str, replace: bool = False) -> bool:
        """True on a hit: the stored snapshot is in build_dir, verified and loadable. A missing or damaged entry,
        and one that does not load on this host, is a miss (logged); build_dir is left as it was then.

        replace: build_dir may be replaced as a whole (it holds nothing but a build)."""
        build_dir = Path(build_dir)
        t0 = time.monotonic()
        man = sim_cache.read_manifest(self.store, self.key)
        if man is None:
            self.damaged = sim_cache.manifest_damaged(self.store, self.key)
            if not self.damaged:            # a damaged manifest is logged already
                self.explain_miss()
            return False
        names = man["files"]
        if kernel(snapshot) not in names:
            self.damaged = True
            log(f"entry {self.key[:16]} has no {kernel(snapshot)}; elaborating")
            return False
        bad = [n for n in names if not storable(n, snapshot)]
        if bad:
            self.damaged = True
            log(f"entry {self.key[:16]} names files that are no part of a snapshot or are outside the build "
                f"directory ({', '.join(bad[:3])}); elaborating")
            return False
        if build_dir.exists() and not replace:
            raise sim_cache.CacheError(f"{build_dir} exists and may not be replaced")
        files = {}
        for name, digest in names.items():
            where = sim_cache.file_name(self.key, digest, ".gz")
            packed = None if where is None else self.store.get(where)
            try:
                data = None if packed is None else gzip.decompress(packed)
            except (OSError, EOFError, zlib.error):
                data = None
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                self.damaged = True
                log(f"entry {self.key[:16]} is damaged ({name}); elaborating")
                return False
            files[name] = data
        build_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=build_dir.parent, prefix=f".{build_dir.name}.restore."))
        try:
            for name, data in files.items():
                (tmp / name).parent.mkdir(parents=True, exist_ok=True)
                (tmp / name).write_bytes(data)
                if is_shared_library(name):
                    (tmp / name).chmod(0o755)
            error = load_error(tmp, snapshot)
            if error is not None:
                meta = man["meta"]
                log(f"entry {self.key[:16]} (elaborated on {meta.get('host', {}).get('os')}) does not load on this "
                    f"host: {error}; elaborating")
                return False
            self.write_stamp(tmp, snapshot, f"restored from {self.store}")
            if build_dir.exists():
                old = Path(tempfile.mkdtemp(dir=build_dir.parent, prefix=f".{build_dir.name}.old."))
                os.replace(build_dir, old / build_dir.name)
                os.replace(tmp, build_dir)
                shutil.rmtree(old, ignore_errors=True)
            else:
                os.replace(tmp, build_dir)
        except OSError as e:
            raise sim_cache.CacheError(f"restore into {build_dir}: {e}") from e
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        meta = man["meta"]
        log(f"hit in {self.store}: {self.build_id} {self.key[:16]} (elaborated {meta.get('stored')} in "
            f"{meta.get('elab_s', 0):.0f} s); restored {len(files)} files ({size(sum(map(len, files.values())))}) "
            f"into {build_dir} in {time.monotonic() - t0:.1f} s; not elaborated")
        return True

    def explain_miss(self) -> None:
        try:
            prev = self.store.get(sim_cache.index_name(self.build_id))
            old = sim_cache.read_manifest(self.store, json.loads(prev)["key"]) if prev else None
        except (ValueError, KeyError, TypeError, sim_cache.CacheError):
            old = None
        if old is None:
            log(f"miss {self.key[:16]}: no earlier entry of {self.build_id} in {self.store}")
            return
        changes = sim_cache.diff(old["inputs"], self.inputs)
        log(f"miss {self.key[:16]}: changed since {old['key'][:16]} ({old['meta'].get('stored')}): "
            + ("; ".join(changes[:20]) + (f"; ... {len(changes) - 20} more" if len(changes) > 20 else "")))

    def save(self, build_dir: Path, snapshot: str, meta: Mapping, overwrite: bool = False) -> None:
        """Stamp build_dir (elab.json) and store its snapshot, the files (by content) first and the manifest last.

        An existing complete entry is kept unless overwrite (SIM_ELAB_CACHE=refresh) or restore() found it
        damaged. A snapshot that names its build directory (link_problem()) is stamped, not stored."""
        build_dir = Path(build_dir)
        digests = self.write_stamp(build_dir, snapshot, "elaborated here")
        problem = link_problem(build_dir, snapshot, self.inputs.get("dpi_libraries", []))
        if problem is not None:
            log(f"{self.key[:16]} not stored: {problem}; it would not run from another directory")
            return
        if self.damaged:
            log(f"replacing the damaged entry {self.key[:16]} in {self.store}")
        elif not overwrite and sim_cache.read_manifest(self.store, self.key) is not None:
            log(f"{self.key[:16]} already stored in {self.store}; kept")
            return
        raw = packed = 0
        for name in digests:
            data = (build_dir / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digests[name]:
                raise sim_cache.CacheError(f"{build_dir / name} changed while it was stored")
            gz = gzip.compress(data, compresslevel=6, mtime=0)
            self.store.put(sim_cache.file_name(self.key, digests[name], ".gz"), gz)
            raw, packed = raw + len(data), packed + len(gz)
        stored = datetime.now(timezone.utc).isoformat(timespec="seconds")
        man = {"key": self.key, "build_id": self.build_id, "inputs": self.inputs, "files": digests,
               "meta": {"stored": stored, "bytes": raw, "stored_bytes": packed, "host": host(), **meta}}
        self.store.put(f"{self.key}/manifest.json", json.dumps(man, indent=1, sort_keys=True).encode())
        self.store.put(sim_cache.index_name(self.build_id), json.dumps({"key": self.key, "stored": stored}).encode())
        log(f"stored {self.key[:16]} in {self.store} ({len(digests)} files, {size(raw)}, {size(packed)} gzipped)")


def cached_elab(build_id: str, inputs_of) -> CachedElab | None:
    """The CachedElab of a build, or None (the reason logged) when SIM_ELAB_CACHE is off or the key cannot be
    made. inputs_of: a function that returns elab_inputs()."""
    try:
        if mode() == "off":
            return None
        return CachedElab(build_id, inputs_of())
    except Exception as e:          # the cache never fails a test
        log(f"disabled for {build_id}: {e}")
        return None


# ---- CLI ----

def main(argv: Sequence[str]) -> int:
    try:
        store = open_store()
        if argv[:1] == ["list"]:
            for key in store.keys():
                man = sim_cache.read_manifest(store, key)
                if man is not None:
                    meta = man["meta"]
                    print(f"{key[:16]}  {meta.get('stored')}  {man.get('build_id')}  {size(meta.get('bytes', 0))}  "
                          f"{meta.get('host', {}).get('os')}")
            return 0
        if len(argv) == 2 and argv[0] == "show":
            print(json.dumps(sim_cache.read_manifest(store, sim_cache.resolve(store, argv[1])), indent=1,
                             sort_keys=True))
            return 0
        if len(argv) == 3 and argv[0] == "diff":
            a, b = (sim_cache.read_manifest(store, sim_cache.resolve(store, k)) for k in argv[1:])
            print("\n".join(sim_cache.diff(a["inputs"], b["inputs"])) or "same inputs")
            return 0
    except sim_cache.CacheError as e:
        print(f"elab_cache: {e}", file=sys.stderr)
        return 1
    print(__doc__.split("CLI")[1].strip(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
