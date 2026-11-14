"""The build-once layer of the characterization bench (characterize/bench/snapshots.py) with a fake build: no
Vivado."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "characterize" / "bench"), str(REPO / "tests")]

import sim_cache  # noqa: E402
import snapshots  # noqa: E402

MODULE = "xfft_core"
SNAP = f"xsim.dir/{MODULE}"


class FakeBuild:
    """Writes a snapshot as xelab would; its xsimk.so follows the source (as a real elaboration's does), the
    other files are the same every time. extra: one more file. dpi: a DPI library to write and return."""

    def __init__(self, dpi: Path | None = None) -> None:
        self.count = 0
        self.dpi = dpi
        self.source = "1"
        self.extra = False

    def __call__(self, root: Path):
        self.count += 1
        snap = root / "xsim.dir" / MODULE
        snap.mkdir(parents=True)
        (snap / "xsimk.so").write_text(f"code of source {self.source}")
        for name in ("xsim.mem", "xsim.reloc", "Compile_Options.txt"):
            (snap / name).write_text(name)
        lib = root / "xsim.dir" / "xil_defaultlib"
        lib.mkdir()
        (lib / "xfft_core.vdb").write_text(f"unit of source {self.source}")
        (lib / "xil_defaultlib.rlx").write_text("rlx")
        (root / "xsim.ini").write_text("xil_defaultlib=xsim.dir/xil_defaultlib")
        if self.extra:
            (snap / "xsim.svtype").write_text("svtype")
        if self.dpi is not None:
            self.dpi.write_text("dpi library")
        return self.dpi


def inputs(sha: str = "1", define: str = "1") -> dict:
    return {"sources": [{"kind": "file", "path": "rtl/a.sv", "sha256": sha}], "defines": {"X": define},
            "toplevel": MODULE, "lang": "verilog", "tools": {"version": "fake"}, "python": {"packages": []}}


@pytest.fixture
def built(tmp_path):
    build = FakeBuild()
    root = tmp_path / "snapshot_rtl"
    return build, root, snapshots.ensure(root, MODULE, inputs(), build)


def test_built_once_and_reused(built, capsys):
    build, root, first = built
    assert first["previous_snapshot_digest"] is None and first["snapshot_digest"] == first["snapshot"]["digest"]
    again = snapshots.ensure(root, MODULE, inputs(), build)
    out = capsys.readouterr().out
    assert build.count == 1 and again == first
    assert f"snapshot {first['snapshot']['digest'][:16]} verified: 4 files identical" in out
    assert sim_cache.make_key(first["build_inputs"]) == first["build_key"]
    assert set(first["snapshot"]["files"]) == {f"{SNAP}/{n}" for n in ("xsimk.so", "xsim.mem", "xsim.reloc",
                                                                         "Compile_Options.txt")}


def test_rebuild_with_the_same_output_reports_every_file_identical(built, capsys):
    build, root, first = built
    stamp = snapshots.ensure(root, MODULE, inputs(define="2"), build)     # changed input, same snapshot
    out = capsys.readouterr().out
    assert build.count == 2 and "~ defines.X: 1 -> 2" in out
    assert "reproduced: 4 files identical" in out
    assert stamp["snapshot_changed"] is False and stamp["changed_files"] == []
    assert stamp["previous_snapshot_digest"] == first["snapshot"]["digest"] == stamp["snapshot_digest"]


def test_changed_source_rebuilds_says_which_and_records_the_change(built, capsys):
    build, root, first = built
    build.source = "2"
    stamp = snapshots.ensure(root, MODULE, inputs("2"), build)
    out = capsys.readouterr().out
    assert build.count == 2 and "build inputs changed" in out and "~ sources[rtl/a.sv].sha256" in out
    assert f"changed: {SNAP}/xsimk.so" in out and "3 files identical" in out
    assert stamp["snapshot_changed"] is True and stamp["changed_files"] == [f"{SNAP}/xsimk.so"]
    assert stamp["previous_snapshot_digest"] == first["snapshot"]["digest"] != stamp["snapshot_digest"]


def test_new_file_joins_the_set(built, capsys):
    build, root, _ = built
    build.extra = True
    stamp = snapshots.ensure(root, MODULE, inputs("3"), build)
    assert f"new {SNAP}/xsim.svtype" in capsys.readouterr().out
    assert f"{SNAP}/xsim.svtype" in stamp["snapshot"]["files"] and stamp["snapshot_changed"] is True


def test_modified_snapshot_file_rebuilds_but_the_kernel_log_does_not(built, capsys):
    build, root, _ = built
    (root / SNAP / "xsimkernel.log").write_text("written by a run")
    snapshots.ensure(root, MODULE, inputs(), build)
    assert build.count == 1
    (root / SNAP / "xsimk.so").write_text("damaged")
    capsys.readouterr()
    snapshots.ensure(root, MODULE, inputs(), build)
    out = capsys.readouterr().out
    assert build.count == 2 and f"snapshot changed: {SNAP}/xsimk.so" in out


def test_unlisted_file_is_reported_but_does_not_invalidate(built, capsys):
    build, root, _ = built
    (root / SNAP / "stray.txt").write_text("x")
    snapshots.ensure(root, MODULE, inputs(), build)
    assert build.count == 1 and f"extra, not checked: {SNAP}/stray.txt" in capsys.readouterr().out


def test_missing_listed_file_rebuilds(built):
    build, root, _ = built
    (root / SNAP / "xsim.reloc").unlink()
    snapshots.ensure(root, MODULE, inputs(), build)
    assert build.count == 2


def test_changed_dpi_library_rebuilds(tmp_path):
    build = FakeBuild(dpi=tmp_path / "dpi_engine" / "libxfft_engine.so")
    (tmp_path / "dpi_engine").mkdir()
    root = tmp_path / "snapshot_engine"
    stamp = snapshots.ensure(root, MODULE, inputs(), build)
    assert "../dpi_engine/libxfft_engine.so" in stamp["snapshot"]["files"]
    (tmp_path / "dpi_engine" / "libxfft_engine.so").write_text("rebuilt elsewhere")
    snapshots.ensure(root, MODULE, inputs(), build)
    assert build.count == 2


def test_damaged_stamp_rebuilds(built):
    build, root, _ = built
    stamp = (root / snapshots.STAMP).read_text().replace('"rtl/a.sv"', '"rtl/b.sv"')    # key no longer matches
    (root / snapshots.STAMP).write_text(stamp)
    snapshots.ensure(root, MODULE, inputs(), build)
    assert build.count == 2


def test_run_dir_is_cleaned_filled_and_verified(built, tmp_path, capsys):
    build, root, stamp = built
    run = tmp_path / "run_a"
    snapshots.prepare_run_dir(root, MODULE, stamp, run)
    assert (run / SNAP / "xsimk.so").read_text() == "code of source 1"
    assert "cleaned 0 files from run dir, verified 4 snapshot files" in capsys.readouterr().out
    # An earlier run's leftovers
    for name in ("xsimkernel.log", f"{SNAP}/xsimkernel.log", "a.result.xml", "sim.log", "xsi.wdb", "case.json",
                 "xelab.pb", "logs/deep/x.txt"):
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text("left over")
    (run / SNAP / "xsim.mem").unlink()                           # a listed file missing
    snapshots.prepare_run_dir(root, MODULE, stamp, run)
    assert "cleaned 8 files from run dir, verified 4 snapshot files" in capsys.readouterr().out
    left = sorted(p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file())
    assert left == [snapshots.RUN_MARKER, *sorted(stamp["snapshot"]["files"])]
    assert not (run / "logs").exists()


def test_mismatching_copy_is_replaced(built, tmp_path, capsys):
    build, root, stamp = built
    run = tmp_path / "run_a"
    snapshots.prepare_run_dir(root, MODULE, stamp, run)
    (run / SNAP / "xsimk.so").write_text("tampered")
    snapshots.prepare_run_dir(root, MODULE, stamp, run)
    out = capsys.readouterr().out
    assert f"snapshot changed: {SNAP}/xsimk.so" in out and "verified 4 snapshot files" in out
    assert (run / SNAP / "xsimk.so").read_text() == "code of source 1"


def test_copy_that_never_verifies_fails(built, tmp_path, monkeypatch):
    build, root, stamp = built

    def broken_copy(src, dst):
        Path(dst).write_text("broken")

    monkeypatch.setattr(snapshots.shutil, "copy2", broken_copy)
    with pytest.raises(snapshots.SnapshotError, match="does not match the stamp"):
        snapshots.prepare_run_dir(root, MODULE, stamp, tmp_path / "run_b")


def test_refuses_to_clean_a_directory_that_is_not_a_run_directory(built):
    build, root, stamp = built
    with pytest.raises(snapshots.SnapshotError, match="not a run directory"):
        snapshots.prepare_run_dir(root, MODULE, stamp, root)       # the build directory itself
    assert (root / snapshots.STAMP).is_file() and (root / SNAP / "xsimk.so").is_file()


LIB = "xsim.dir/xil_defaultlib"


def test_libraries_are_listed_and_compared(built, capsys):
    build, root, first = built
    assert set(first["libraries"]["files"]) == {f"{LIB}/xfft_core.vdb", f"{LIB}/xil_defaultlib.rlx", "xsim.ini"}
    assert first["libraries_changed"] is None
    snapshots.ensure(root, MODULE, inputs(), build)                    # unchanged: reused, no rebuild
    out = capsys.readouterr().out
    assert build.count == 1 and "libraries" in out and "verified: 3 files identical" in out
    build.source = "2"
    stamp = snapshots.ensure(root, MODULE, inputs("2"), build)
    assert stamp["libraries_changed"] is True and stamp["changed_library_files"] == [f"{LIB}/xfft_core.vdb"]
    assert stamp["previous_libraries_digest"] == first["libraries"]["digest"]


def test_modified_library_unit_rebuilds_from_scratch(built, capsys):
    build, root, _ = built
    (root / LIB / "xfft_core.vdb").write_text("stale")
    snapshots.ensure(root, MODULE, inputs(), build)
    out = capsys.readouterr().out
    assert build.count == 2 and f"libraries changed: {LIB}/xfft_core.vdb" in out


def test_extra_library_unit_rebuilds_from_scratch(built, capsys):
    build, root, _ = built
    (root / LIB / "removed_source.sdb").write_text("left over")
    snapshots.ensure(root, MODULE, inputs(), build)
    out = capsys.readouterr().out
    assert build.count == 2 and f"extra units: {LIB}/removed_source.sdb" in out
