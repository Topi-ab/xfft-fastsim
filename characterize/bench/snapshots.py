"""Build once, run many: the elaborated design of a model, shared by all cases of the characterization bench.

cocotb-vivado elaborates a snapshot that any number of cocotb runs can use. The repo's cached runner instead
builds from scratch on every cache miss (so that a stale snapshot never passes under a new key). Here a snapshot
is built once per build key, the part of the cache key that decides the snapshot, and checked before each use.

The stamp, snapshot.json in the snapshot directory, is written right after a successful build, before any
simulation. It has three parts:
- build_inputs: the manifest of everything the build used, the elaboration cache's (elab_cache.elab_inputs():
  the sources with repo-relative paths and sha256 in compile order, the stand-in's files and constants, the IP
  builder Tcl, the defines and toplevel, the tool fingerprint, the Vivado path, cocotb and cocotb-vivado);
- build_key: the sha256 of the canonical build_inputs (asserted whenever a stamp is read or written);
- snapshot: the explicit list of the snapshot's files with their sha256, relative to the snapshot directory:
  xsim.dir/<snapshot>/... and what a run needs from dpi/ (the stand-in's libxfft_engine.so, the C model's shared
  libraries, the header and constants_source.txt the bench reads), plus one digest over them. The list defines
  what the snapshot is: every check hashes exactly the listed files.
- libraries: the same for what the elaboration reads from the build directory: the compiled libraries
  (xsim.dir/<library>/, e.g. xil_defaultlib) and xsim.ini. Verified before the build directory is reused; a
  changed or missing file, or a compiled unit the list lacks (.vdb, .sdb, .rlx: e.g. left over from a removed
  source), means a rebuild from scratch. They stay in the build directory; run directories get the snapshot.
Snapshots go into the elaboration cache (elab_cache.py, SIM_ELAB_CACHE): ensure() restores a stored one with
the same build key instead of building, and stores what it builds. A restored snapshot has no libraries.

ensure(): a snapshot with the same build key is reused when all listed files verify; a listed file that is
missing or changed means a rebuild. Files in xsim.dir/<snapshot>/ that the list does not have are logged, not
fatal. A rebuild logs why (the build inputs that changed, as the cache explains a miss, or the files that do not
match), then compares the old list's files in the new build with their old hashes (which changed, which stayed
identical: how reproducible the elaboration is) and writes the new list: the old one's files plus the new
build's new files, the set changes logged. XSim's runtime files (VOLATILE) are never listed. A lock serialises
parallel workers.
prepare_run_dir(): each case runs in its own run directory with a copy of the snapshot and of dpi/ (XSI loads
the snapshot relative to the test directory, the snapshot links ./dpi/libxfft_engine.so, and XSim writes into the
snapshot directory, so a copy, not a link). Before every run everything the file list
does not have is deleted, missing listed files are copied and every listed file is verified (fail closed). Only
directories marked as run directories are cleaned, never the build directory with its intermediates.
The digest is for provenance only (the cache entry's meta), not part of any key: snapshots need not be
bit-reproducible.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import elab_cache
import sim_cache

STAMP = "snapshot.json"
# Files XSim writes into the snapshot directory while a simulation runs (checked by hashing before and after)
VOLATILE = {"xsimkernel.log"}
# Prefixes of the listed files a run directory gets
RUN_PREFIXES = ("xsim.dir/", f"{elab_cache.DPI_DIR}/")
# Compiled design units in xsim.dir/<library>/: an unlisted one (left over from a removed source) is fatal
UNIT_SUFFIXES = {".vdb", ".sdb", ".rlx"}


class SnapshotError(Exception):
    """A copy of a snapshot does not match its stamp."""


def log(msg: str) -> None:
    print(f"snapshots: {msg}", flush=True)


def build_inputs(sources: list, defines: dict, toplevel: str, library: str) -> dict:
    """Everything that decides the snapshot, as the elaboration cache keys it (elab_cache.elab_inputs()): the
    sources (with the stand-in's files and the IP builder Tcl), the defines, the toplevel, the tools, cocotb and
    cocotb-vivado. Not the variation, seed or test code, so a snapshot of the flow tests serves here too."""
    return elab_cache.elab_inputs(sources=sources, hdl_toplevel=toplevel, hdl_library=library,
                                  verilog_defines=defines)


def library_files(root: Path, module: str) -> set[str]:
    """What the elaboration reads from the build directory besides the sources: the compiled libraries
    (xsim.dir/<library>/, every directory but the snapshot's) and xsim.ini, relative to root."""
    out = {"xsim.ini"} if (root / "xsim.ini").is_file() else set()
    for lib in sorted((root / "xsim.dir").glob("*")):
        if lib.is_dir() and lib.name != module:
            out |= {p.relative_to(root).as_posix() for p in lib.rglob("*") if p.is_file() and p.name not in VOLATILE}
    return out


def verify_libraries(root: Path, module: str, stamp: dict) -> list[str]:
    """Recompute the listed library files' hashes and compare them with the stamp; log the outcome. The
    problems (empty: verified): a changed or missing file, or a compiled unit the list does not have."""
    listed = stamp["libraries"]["files"]
    now = hashes(root, listed)
    changed = [n for n, h in listed.items() if now[n] is not None and now[n] != h]
    missing = [n for n in listed if now[n] is None]
    extra = sorted(library_files(root, module) - set(listed))
    units = [n for n in extra if Path(n).suffix in UNIT_SUFFIXES]
    if not changed and not missing and not units:
        log(f"{root.name}: libraries {stamp['libraries']['digest'][:16]} verified: {len(listed)} files identical"
            + (f"; extra, not checked: {', '.join(extra)}" if extra else ""))
        return []
    log(f"{root.name}: libraries changed: " + ", ".join(f"{n}: {listed[n][:12]} -> {now[n][:12]}" for n in changed)
        + f"; missing: {', '.join(missing) or '-'}; extra units: {', '.join(units) or '-'}")
    return [f"{n}: changed" for n in changed] + [f"{n}: missing" for n in missing] + [f"{n}: extra unit" for n in units]


def compare_lists(name: str, before: dict, root: Path, names: set[str]) -> tuple[set[str], dict]:
    """The old list's files in a new build: log which changed and which stayed identical; the new file set (the
    old one's files that are still there plus the new ones) and the comparison for the stamp."""
    after = hashes(root, before)
    changed = sorted(n for n in before if after[n] is not None and after[n] != before[n])
    same = sorted(n for n in before if after[n] == before[n])
    gone = sorted(n for n in before if after[n] is None)
    new = sorted(names - set(before))
    if changed or gone:
        log(f"{root.name}: rebuilt: {name} changed: {len(same)} files identical, changed: "
            + ", ".join(f"{n}: {before[n][:12]} -> {after[n][:12]}" for n in changed)
            + (f"; gone: {', '.join(gone)}" if gone else ""))
    else:
        log(f"{root.name}: rebuilt: {name} reproduced: {len(same)} files identical")
    if new or gone:
        log(f"{root.name}: {name} file set changed: new {', '.join(new) or '-'}; removed {', '.join(gone) or '-'}")
    return (set(before) - set(gone)) | names, {"changed_files": changed + gone + new}


def present(root: Path, module: str) -> set[str]:
    """The non-volatile files now in root/xsim.dir/<module>/, relative to root."""
    snap = root / "xsim.dir" / module
    return {p.relative_to(root).as_posix() for p in snap.rglob("*") if p.is_file() and p.name not in VOLATILE}


def hashes(root: Path, names) -> dict[str, str | None]:
    """sha256 of the named files (relative to root), None for a missing one."""
    return {n: sim_cache.sha256_file(root / n) if (root / n).is_file() else None for n in sorted(names)}


def digest(files: dict) -> str:
    return hashlib.sha256(sim_cache.canonical(files).encode()).hexdigest()


def read_stamp(root: Path) -> dict | None:
    try:
        stamp = json.loads((root / STAMP).read_text())
        assert sim_cache.make_key(stamp["build_inputs"]) == stamp["build_key"], "build_key != sha256(build_inputs)"
        stamp["snapshot"]["files"], stamp["libraries"]["files"]
        return stamp
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as e:
        if (root / STAMP).exists():
            log(f"{root / STAMP} is not usable ({e})")
        return None


def verify(root: Path, module: str, stamp: dict, copy: bool = False) -> list[str]:
    """Recompute the listed files' hashes under root and compare them with the stamp, per file and as one
    digest; log the outcome in one line. The missing or changed files (empty: verified). copy: root is a case's
    run directory, which has the xsim.dir and dpi files only. Unlisted files are logged, not returned."""
    listed = {n: h for n, h in stamp["snapshot"]["files"].items() if not copy or n.startswith(RUN_PREFIXES)}
    now = hashes(root, listed)
    changed = [f"{n}: {h[:12]} -> {now[n][:12]}" for n, h in listed.items() if now[n] is not None and now[n] != h]
    missing = [n for n in listed if now[n] is None]
    extra = sorted(present(root, module) - set(stamp["snapshot"]["files"]))
    ok = not changed and not missing and digest({n: now[n] for n in listed}) == digest(listed)
    where = f"{root.name}{' (copy)' if copy else ''}"
    if ok:
        log(f"{where}: snapshot {stamp['snapshot']['digest'][:16]} verified: {len(listed)} files identical"
            + (f"; extra, not checked: {', '.join(extra)}" if extra else ""))
    else:
        log(f"{where}: snapshot changed: {', '.join(changed) or '-'}; missing: {', '.join(missing) or '-'}"
            + (f"; extra, not checked: {', '.join(extra)}" if extra else ""))
    return [c.split(":")[0] + ": changed" for c in changed] + [f"{n}: missing" for n in missing]


def ensure(root: Path, module: str, inputs: dict, build: Callable[[Path], Path | None],
           elab: elab_cache.CachedElab | None = None) -> dict:
    """The stamp of a verified snapshot under root built from these build inputs; restored from the elaboration
    cache or built (from scratch) when needed, the reason and the effect logged.

    build(root) builds into root and returns the DPI library the snapshot links to (when it is outside root), or
    None. elab: the elaboration cache entry of these inputs (None: no cache)."""
    key = sim_cache.make_key(inputs)
    root.parent.mkdir(parents=True, exist_ok=True)
    with open(root.parent / f"{root.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read_stamp(root)
        if old is None:
            log(f"{root.name}: no build here yet")
        elif old["build_key"] != key:
            changes = sim_cache.diff(old["build_inputs"], inputs)
            log(f"{root.name}: build inputs changed ({'; '.join(changes[:10])}"
                + (f"; ... {len(changes) - 10} more" if len(changes) > 10 else "") + "): rebuilding")
        else:
            # Before reusing (or building incrementally on) a build directory: the snapshot and the compiled
            # libraries must both match the stamp
            problems = verify(root, module, old) + verify_libraries(root, module, old)
            if not problems:
                return old
            log(f"{root.name}: does not match its stamp ({'; '.join(problems[:5])}): rebuilding from scratch")
        (root / STAMP).unlink(missing_ok=True)
        if elab is not None:
            assert elab.key == key, "the elaboration cache entry is not of these build inputs"
            if elab_cache.mode() == "on":
                try:
                    if elab.restore(root, module, replace=True):
                        return write_restored_stamp(root, module, inputs, key, old, str(elab.store))
                except Exception as e:      # the cache never fails a run
                    elab_cache.log(f"lookup failed, building: {e}")
        shutil.rmtree(root, ignore_errors=True)
        start = time.monotonic()
        library = build(root)
        elab_s = time.monotonic() - start
        names = set(elab_cache.runtime_files(root, module))
        if library is not None:
            names.add(os.path.relpath(library, root))
        lib_names = library_files(root, module)
        comparison = {"previous_snapshot_digest": None, "snapshot_changed": None, "changed_files": [],
                      "previous_libraries_digest": None, "libraries_changed": None, "changed_library_files": []}
        if old is not None:
            # The old lists' files in the new build: what the rebuild changed, what it reproduced
            names, snap_cmp = compare_lists("snapshot", old["snapshot"]["files"], root, names)
            lib_names, lib_cmp = compare_lists("libraries", old["libraries"]["files"], root, lib_names)
            comparison.update({"previous_snapshot_digest": old["snapshot"]["digest"],
                               "changed_files": snap_cmp["changed_files"],
                               "previous_libraries_digest": old["libraries"]["digest"],
                               "changed_library_files": lib_cmp["changed_files"]})
        files = {n: h for n, h in hashes(root, names).items() if h is not None}
        snapshot = {"files": files, "digest": digest(files)}
        lib_files = {n: h for n, h in hashes(root, lib_names).items() if h is not None}
        libraries = {"files": lib_files, "digest": digest(lib_files)}
        if old is not None:
            comparison["snapshot_changed"] = snapshot["digest"] != old["snapshot"]["digest"]
            comparison["libraries_changed"] = libraries["digest"] != old["libraries"]["digest"]
        stamp = {"build_inputs": inputs, "build_key": key, "snapshot": snapshot, "libraries": libraries,
                 "snapshot_digest": snapshot["digest"], **comparison}
        assert sim_cache.make_key(stamp["build_inputs"]) == stamp["build_key"]
        (root / STAMP).write_text(json.dumps(stamp, indent=1, sort_keys=True) + "\n")
        if elab is not None:
            try:
                elab.save(root, module, {"elab_s": elab_s}, overwrite=elab_cache.mode() == "refresh")
            except Exception as e:          # the cache never fails a run
                elab_cache.log(f"store failed: {e}")
        return stamp


def write_restored_stamp(root: Path, module: str, inputs: dict, key: str, old: dict | None, origin: str) -> dict:
    """The stamp of a snapshot restored from the elaboration cache: its run-time files; no libraries (a restored
    snapshot has none, and needs none)."""
    files = hashes(root, elab_cache.runtime_files(root, module))
    snapshot = {"files": files, "digest": digest(files)}
    stamp = {"build_inputs": inputs, "build_key": key, "snapshot": snapshot,
             "libraries": {"files": {}, "digest": digest({})}, "snapshot_digest": snapshot["digest"],
             "restored_from": origin,
             "previous_snapshot_digest": None if old is None else old["snapshot"]["digest"],
             "snapshot_changed": None if old is None else snapshot["digest"] != old["snapshot"]["digest"],
             "changed_files": [], "previous_libraries_digest": None, "libraries_changed": None,
             "changed_library_files": []}
    (root / STAMP).write_text(json.dumps(stamp, indent=1, sort_keys=True) + "\n")
    log(f"{root.name}: restored snapshot {snapshot['digest'][:16]} ({len(files)} files) from {origin}")
    return stamp


RUN_MARKER = ".run_dir"


def mark_run_dir(run_dir: Path) -> None:
    """Make run_dir a run directory: only those may be cleaned (never a build directory)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_MARKER).write_text("a run directory of the characterization bench\n")


def prepare_run_dir(root: Path, module: str, stamp: dict, run_dir: Path) -> None:
    """Get run_dir ready for a run of the snapshot under root: create it (or reuse it), delete everything the
    stamp's file list does not have (an earlier run's kernel log, result XML, logs, waveform database, the case's
    output), copy the listed files that are missing, verify every listed file (a mismatching one is copied again,
    once; then it fails)."""
    if run_dir.exists() and any(run_dir.iterdir()) and not (run_dir / RUN_MARKER).is_file():
        raise SnapshotError(f"{run_dir} is not a run directory (no {RUN_MARKER}): refusing to clean it")
    mark_run_dir(run_dir)
    listed = {n for n in stamp["snapshot"]["files"] if n.startswith(RUN_PREFIXES)}
    removed = 0
    for p in sorted(run_dir.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        rel = p.relative_to(run_dir).as_posix()
        if p.is_symlink() or p.is_file():
            if rel not in listed and rel != RUN_MARKER:
                p.unlink()
                removed += 1
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    for attempt in (1, 2):
        for n in sorted(listed):
            if attempt == 2 or not (run_dir / n).is_file():
                (run_dir / n).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / n, run_dir / n)
        problems = verify(run_dir, module, stamp, copy=True)
        if not problems:
            log(f"{run_dir.name}: cleaned {removed} files from run dir, verified {len(listed)} snapshot files")
            return
        bad = {pr.split(":")[0] for pr in problems}
        for n in bad:
            (run_dir / n).unlink(missing_ok=True)         # copied again in the second attempt
    raise SnapshotError(f"copy of {root.name} in {run_dir} does not match the stamp: {'; '.join(problems[:3])}")
