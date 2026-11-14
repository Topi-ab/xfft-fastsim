"""Elaboration cache (tests/elab_cache.py), its use in sim_runner.run() and in the characterization's snapshots.

Runs without a simulator: fake snapshots, the S3 store against moto, sim_runner with a stand-in for the
cocotb-vivado runner, the load check and the link check stubbed (each also run for real: the load check on a
file that is no library, the link check on small ELF files written here).
    pytest tests/unit
"""

import gzip
import json
import shutil
import struct
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TESTS), str(TESTS.parent / "characterize" / "bench")]

import elab_cache  # noqa: E402
import sim_cache  # noqa: E402

SNAP = "top"
KERNEL = f"xsim.dir/{SNAP}/xsimk.so"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A stand-in repository and Vivado: sim_cache sees tmp_path as the repository root."""
    monkeypatch.setattr(sim_cache, "REPO", tmp_path)
    monkeypatch.setattr(sim_cache, "tool_fingerprint", lambda sim: {"sim": sim, "version": "fake 1.0"})
    monkeypatch.setattr(elab_cache, "load_error", lambda run_dir, snapshot: None)
    monkeypatch.setattr(elab_cache, "link_problem", lambda build_dir, snapshot, dpi_libraries: None)
    monkeypatch.setenv("XILINX_VIVADO", str(tmp_path / "vivado"))
    for name in ("SIM_CACHE", "SIM_ELAB_CACHE", "SIM_ELAB_CACHE_URL", "WAVES", *elab_cache.COMPILER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SIM_CACHE_URL", str(tmp_path / ".sim_cache"))
    return tmp_path


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.write_bytes if isinstance(data, bytes) else path.write_text)(data)
    return path


def fake_build(build_dir: Path, code: str = "1") -> Path:
    """What an elaboration of the stand-in leaves: the snapshot, the DPI directory, libraries, logs, the IP."""
    write(build_dir / KERNEL, f"kernel {code}")
    write(build_dir / f"xsim.dir/{SNAP}/xsim.mem", b"\0" * 1000)
    write(build_dir / f"xsim.dir/{SNAP}/xsimkernel.log", "written by every run")
    write(build_dir / "xsim.dir/xil_defaultlib/top.sdb", "compiled unit")
    write(build_dir / "xsim.ini", "xil_defaultlib=xsim.dir/xil_defaultlib")
    write(build_dir / "dpi/libxfft_engine.so", "dpi library")
    write(build_dir / "dpi/xfft_cmodel_api.h", "#define XFFT_API_PREFIX xfft_v9_1")
    write(build_dir / "dpi/constants_source.txt", "TABLE")
    write(build_dir / "dpi/cc.log", "compiled")
    write(build_dir / "dpi/cmodel/libIp_xfft_v9_1_bitacc_cmodel.so", "c model")
    write(build_dir / "dpi/cmodel/libgmp.so.11", "gmp")
    write(build_dir / "dpi/cmodel/xfft_v9_1_bitacc_cmodel.h", "header of the archive")
    write(build_dir / "ip/top/top.xci", "xilinx.com:ip:xfft:9.1")
    write(build_dir / "ip/top/sim/top.vhd", "generated")
    write(build_dir / "build.log", "xelab: built")
    return build_dir


STORED = sorted([KERNEL, f"xsim.dir/{SNAP}/xsim.mem", "dpi/libxfft_engine.so", "dpi/xfft_cmodel_api.h",
                 "dpi/constants_source.txt", "dpi/cmodel/libIp_xfft_v9_1_bitacc_cmodel.so", "dpi/cmodel/libgmp.so.11",
                 "ip/top/top.xci", "build.log"])


def stored_file(e: elab_cache.CachedElab, name: str) -> Path:
    """The gzipped file of a directory store's entry that the manifest maps name to."""
    digest = json.loads(e.store.get(f"{e.key}/manifest.json"))["files"][name]
    return e.store.root / sim_cache.file_name(e.key, digest, ".gz")


def key_inputs(repo: Path, text: str = "module top; endmodule", **kw) -> dict:
    src = write(repo / "rtl" / "top.sv", text)
    return elab_cache.elab_inputs(sources=[src], hdl_toplevel=SNAP, hdl_library="xil_defaultlib",
                                  **{"verilog_defines": {"X": 1}, **kw})


def elab(repo: Path, store=None, build_id: str = "cfg/flow/vivado", **kw) -> elab_cache.CachedElab:
    return elab_cache.CachedElab(build_id, key_inputs(repo, **kw),
                                 store=store if store is not None else elab_cache.open_store())


# ---- Key and file set ----

def test_key_holds_the_build_inputs_and_not_the_place(repo, monkeypatch):
    k = elab(repo).key
    assert elab(repo, build_id="elsewhere").key == k                  # the build's name is not in the key
    assert elab(repo, verilog_defines={"X": 2}).key != k
    assert elab(repo, text="module top(); endmodule").key != k
    assert elab(repo, parameters={"N": 8}).key != k
    monkeypatch.setenv("XILINX_VIVADO", str(repo / "other_vivado"))   # the snapshot's rpath names the install
    assert elab(repo).key != k
    monkeypatch.setenv("XILINX_VIVADO", str(repo / "vivado"))
    monkeypatch.setenv("CPATH", "/opt/include")                       # changes what the compiler sees
    assert elab(repo).key != k
    inputs = key_inputs(repo)
    assert inputs["sources"][0]["path"] == "rtl/top.sv" and inputs["layout"] == elab_cache.LAYOUT
    assert set(inputs["packages"]) == set(elab_cache.BUILD_DISTS)
    assert not any(str(repo) in json.dumps(v) for k, v in inputs.items() if k != "vivado_root")


def test_stored_and_runtime_files(repo):
    b = fake_build(repo / "b")
    assert elab_cache.stored_files(b, SNAP) == STORED
    assert elab_cache.runtime_files(b, SNAP) == [n for n in STORED if n.startswith(("xsim.dir/", "dpi/"))]


def elf(needed=(), rpath: str | None = None) -> bytes:
    """A minimal 64-bit little-endian ELF file with a dynamic section: one PT_LOAD over the whole file at
    address 0x1000, PT_DYNAMIC, DT_NEEDED entries, DT_RPATH and DT_STRTAB."""
    strtab = b"\0"
    offsets = []
    for s in (*needed, *([rpath] if rpath is not None else [])):
        offsets.append(len(strtab))
        strtab += s.encode() + b"\0"
    tags = [(1, o) for o in offsets[:len(needed)]] + ([(15, offsets[-1])] if rpath is not None else [])
    header, phdrs = 64, 2 * 56
    dyn_off = header + phdrs
    dyn_size = (len(tags) + 2) * 16
    str_off = dyn_off + dyn_size
    base = 0x1000
    dynamic = b"".join(struct.pack("<qQ", t, v) for t, v in [*tags, (5, base + str_off)]) + struct.pack("<qQ", 0, 0)
    size = str_off + len(strtab)
    ehdr = (b"\x7fELF\x02\x01\x01" + b"\0" * 9 + struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, header, 0, 0, 64, 56,
                                                                2, 64, 0, 0))
    ph_load = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, size, size, 0x1000)
    ph_dyn = struct.pack("<IIQQQQQQ", 2, 6, dyn_off, base + dyn_off, base + dyn_off, dyn_size, dyn_size, 8)
    return ehdr + ph_load + ph_dyn + dynamic + strtab


def test_dynamic_entries_and_link_problem(tmp_path):
    """The real link check (no fixture stubs it here)."""
    b = tmp_path / "build" / "vivado"
    write(b / KERNEL, elf(["libxv_simulator_kernel.so", "./dpi/libxfft_engine.so"], "/opt/vivado/lib"))
    write(b / "dpi/libxfft_engine.so", elf(["libc.so.6"], "$ORIGIN/cmodel:/opt/vivado/lib"))
    assert elab_cache.dynamic_entries(b / KERNEL) == {
        "needed": ["libxv_simulator_kernel.so", "./dpi/libxfft_engine.so"], "rpath": ["/opt/vivado/lib"],
        "runpath": []}
    assert elab_cache.link_problem(b, SNAP, ["libxfft_engine"]) is None
    write(b / KERNEL, elf([f"{b}/dpi/libxfft_engine.so"]))          # -sv_root outside, or another spelling
    assert "absolute path" in elab_cache.link_problem(b, SNAP, ["libxfft_engine"])
    write(b / KERNEL, elf(["libother.so"]))
    assert "does not link libxfft_engine.so" in elab_cache.link_problem(b, SNAP, ["libxfft_engine"])
    write(b / KERNEL, elf(["./dpi/libxfft_engine.so"]))
    write(b / "dpi/libxfft_engine.so", elf(["libc.so.6"], f"{b.resolve()}/dpi/cmodel"))
    assert "names the build directory in its RPATH" in elab_cache.link_problem(b, SNAP, ["libxfft_engine"])
    write(b / KERNEL, "not an ELF file")
    assert "no readable dynamic section" in elab_cache.link_problem(b, SNAP, [])


def test_save_refuses_a_snapshot_that_names_its_build_directory(repo, monkeypatch, capsys):
    monkeypatch.setattr(elab_cache, "link_problem", lambda build_dir, snapshot, dpi_libraries: "it does")
    e = elab(repo)
    built = fake_build(repo / "build" / "a")
    e.save(built, SNAP, {"elab_s": 1.0})
    assert "not stored: it does" in capsys.readouterr().out
    assert e.store.keys() == [] and e.local(built, SNAP)          # stamped: reused here, never shared


def test_load_error_reports_a_file_that_does_not_load(tmp_path):
    """The real check (no fixture stubs it here)."""
    write(tmp_path / KERNEL, "not a shared library")
    error = elab_cache.load_error(tmp_path, SNAP)
    assert error is not None and "xsimk.so" in error


# ---- Stores and entries ----

def check_round_trip(repo: Path, store, capsys) -> None:
    built = fake_build(repo / "build" / "a")
    e = elab(repo, store)
    assert not e.restore(repo / "build" / "b", SNAP)
    assert "no earlier entry of cfg/flow/vivado" in capsys.readouterr().out
    e.save(built, SNAP, {"elab_s": 16.0})
    assert json.loads((built / elab_cache.STAMP).read_text())["key"] == e.key
    assert e.local(built, SNAP)

    other = repo / "elsewhere" / "vivado"                  # another checkout, another machine
    assert elab(repo, store).restore(other, SNAP)
    out = capsys.readouterr().out
    assert "hit in" in out and "restored 9 files" in out and "not elaborated" in out
    left = sorted(p.relative_to(other).as_posix() for p in other.rglob("*") if p.is_file())
    assert left == sorted([*STORED, elab_cache.STAMP])
    assert (other / KERNEL).read_text() == "kernel 1"
    assert elab(repo, store).local(other, SNAP)
    assert not list(other.parent.glob(".vivado.*"))        # no temporary directory left

    miss = elab(repo, store, verilog_defines={"X": 3})
    assert not miss.restore(repo / "build" / "c", SNAP)
    out = capsys.readouterr().out
    assert f"changed since {e.key[:16]}" in out and "~ verilog_defines.X: 1 -> 3" in out


def test_dir_store_round_trip(repo, capsys):
    check_round_trip(repo, elab_cache.open_store(), capsys)
    root = repo / ".sim_cache"
    assert (root / "elab" / "index").is_dir()
    key = next(p.name for p in (root / "elab").iterdir() if p.name != "index")
    e = elab_cache.CachedElab("x", {}, store=elab_cache.open_store())
    e.key = key
    assert gzip.decompress(stored_file(e, KERNEL).read_bytes()) == b"kernel 1"
    assert sim_cache.DirStore(root).keys() == []           # the result cache's listing does not see them
    assert elab_cache.open_store().keys() == [key]


def test_s3_store_round_trip(repo, capsys, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="results",
                                         CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        monkeypatch.setenv("SIM_CACHE_URL", "s3://results/fft/sim")
        store = elab_cache.open_store()
        assert str(store) == "s3://results/fft/sim/elab/"
        check_round_trip(repo, store, capsys)
        keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="results")["Contents"]]
        assert all(k.startswith("fft/sim/elab/") for k in keys)
        assert sim_cache.open_store().keys() == [] and len(store.keys()) == 1
        monkeypatch.setenv("SIM_ELAB_CACHE_URL", str(repo / "elab_store"))     # a store of its own
        assert isinstance(elab_cache.open_store(), sim_cache.DirStore)


def saved(repo: Path) -> elab_cache.CachedElab:
    e = elab(repo)
    e.save(fake_build(repo / "build" / "a"), SNAP, {"elab_s": 1.0})
    return e


def test_damaged_file_is_a_miss_and_leaves_the_directory(repo, capsys):
    e = saved(repo)
    target = write(repo / "build" / "b" / "keep.txt", "an earlier build")
    gz = stored_file(e, KERNEL)
    gz.write_bytes(gzip.compress(b"other code"))
    assert not e.restore(target.parent, SNAP, replace=True)
    assert "is damaged (xsim.dir/top/xsimk.so)" in capsys.readouterr().out
    gz.write_bytes(b"not gzip")
    assert not e.restore(target.parent, SNAP, replace=True)
    assert "is damaged" in capsys.readouterr().out
    assert target.read_text() == "an earlier build" and not (target.parent / KERNEL).exists()


def test_damaged_or_missing_manifest_is_a_miss(repo, capsys):
    e = saved(repo)
    e.store.put(f"{e.key}/manifest.json", b"{not json")
    assert not e.restore(repo / "build" / "b", SNAP)
    assert "damaged manifest" in capsys.readouterr().out
    (e.store.root / e.key / "manifest.json").unlink()      # a store interrupted before the manifest
    assert not e.restore(repo / "build" / "b", SNAP)
    assert e.store.keys() == [] and not (repo / "build" / "b").exists()


def test_names_outside_the_build_directory_are_refused(repo, capsys):
    e = saved(repo)
    man = json.loads(e.store.get(f"{e.key}/manifest.json"))
    man["files"]["../../escaped.so"] = man["files"][KERNEL]
    e.store.put(f"{e.key}/manifest.json", json.dumps(man).encode())
    assert not e.restore(repo / "build" / "b", SNAP)
    assert "outside the build directory" in capsys.readouterr().out
    assert not (repo / "escaped.so").exists()


def test_names_that_are_no_part_of_a_snapshot_are_refused(repo, capsys):
    """A stored cocotb.py or .pth would shadow Python imports in the test directory."""
    e = saved(repo)
    for name in ("cocotb.py", "x.pth", "xsim.dir/other/xsimk.so", "dpi/cmodel/a.h", "ip/top/sim/top.vhd"):
        assert not elab_cache.storable(name, SNAP), name
    assert all(elab_cache.storable(n, SNAP) for n in STORED)
    man = json.loads(e.store.get(f"{e.key}/manifest.json"))
    man["files"]["cocotb.py"] = man["files"]["build.log"]
    e.store.put(f"{e.key}/manifest.json", json.dumps(man).encode())
    assert not e.restore(repo / "build" / "b", SNAP)
    assert "no part of a snapshot" in capsys.readouterr().out and not (repo / "build" / "b").exists()


def test_concurrent_writers_leave_a_complete_entry(repo):
    """Two builders miss and store one key at the same time; the manifest that lands last is the slower
    builder's, and its files are all there (they are stored by content, the other builder's beside them)."""
    store = elab_cache.open_store()

    class Deferred:
        """The store as builder A sees it: its manifest and index land only when released."""

        def __init__(self) -> None:
            self.held = []

        def get(self, name):
            return store.get(name)

        def put(self, name, data):
            if name.endswith("manifest.json") or name.startswith("index/"):
                self.held.append((name, data))
            else:
                store.put(name, data)

    deferred = Deferred()
    a = elab_cache.CachedElab("cfg/flow/vivado", key_inputs(repo), store=deferred)
    a.save(fake_build(repo / "a", code="A"), SNAP, {"elab_s": 1.0})            # files written, manifest held
    b = elab(repo, store)
    b.save(fake_build(repo / "b", code="B"), SNAP, {"elab_s": 1.0})            # complete
    for name, data in deferred.held:                                            # A's manifest lands last
        store.put(name, data)
    assert elab(repo, store).restore(repo / "c", SNAP)
    assert (repo / "c" / KERNEL).read_text() == "kernel A"


def test_damaged_entry_is_replaced_by_the_next_build(repo, capsys):
    e = saved(repo)
    stored_file(e, KERNEL).write_bytes(gzip.compress(b"damaged"))
    again = elab(repo)
    assert not again.restore(repo / "build" / "b", SNAP)
    again.save(fake_build(repo / "build" / "b", code="2"), SNAP, {"elab_s": 1.0})
    assert "replacing the damaged entry" in capsys.readouterr().out
    assert elab(repo).restore(repo / "build" / "c", SNAP)
    assert (repo / "build" / "c" / KERNEL).read_text() == "kernel 2"


def test_damaged_manifest_is_replaced_and_logged_once(repo, capsys):
    e = saved(repo)
    e.store.put(f"{e.key}/manifest.json", b"{not json")
    again = elab(repo)
    assert not again.restore(repo / "build" / "b", SNAP)
    again.save(fake_build(repo / "build" / "b"), SNAP, {"elab_s": 1.0})
    assert capsys.readouterr().out.count("damaged manifest") == 1
    assert elab(repo).restore(repo / "build" / "c", SNAP)


def test_entry_without_the_snapshot_is_a_miss(repo, capsys):
    e = saved(repo)
    assert not e.restore(repo / "build" / "b", "other_top")
    assert "has no xsim.dir/other_top/xsimk.so" in capsys.readouterr().out


def test_snapshot_that_does_not_load_is_a_miss(repo, monkeypatch, capsys):
    e = saved(repo)
    target = write(repo / "build" / "b" / "keep.txt", "an earlier build").parent
    error = "libxv_xsim_systemc.so: version `GLIBCXX_3.4.26' not found"
    monkeypatch.setattr(elab_cache, "load_error", lambda run_dir, snapshot: error)
    assert not e.restore(target, SNAP, replace=True)
    assert f"does not load on this host: {error}; elaborating" in capsys.readouterr().out
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
    assert not list(target.parent.glob(".b.*"))             # the temporary directory is removed
    assert e.store.keys() == [e.key]                        # the entry stays: another host may load it


def test_restore_does_not_replace_unless_allowed(repo):
    e = saved(repo)
    write(repo / "build" / "b" / "keep.txt", "x")
    with pytest.raises(sim_cache.CacheError):
        e.restore(repo / "build" / "b", SNAP)


def test_local_snapshot_is_verified(repo, capsys):
    e = saved(repo)
    built = repo / "build" / "a"
    write(built / f"xsim.dir/{SNAP}/xsimkernel.log", "a run")             # volatile: not checked
    write(built / "r.result.xml", "<testsuites/>")                         # a run's output: not listed
    assert e.local(built, SNAP)
    write(built / KERNEL, "changed")
    assert not e.local(built, SNAP)
    assert "does not verify (xsim.dir/top/xsimk.so)" in capsys.readouterr().out
    assert not elab(repo, verilog_defines={"X": 9}).local(built, SNAP)    # another key


def test_save_keeps_an_entry_unless_overwrite(repo, capsys):
    e = saved(repo)
    built = repo / "build" / "a"
    write(built / KERNEL, "kernel 2")
    e.save(built, SNAP, {"elab_s": 1.0})
    assert "kept" in capsys.readouterr().out
    assert gzip.decompress(stored_file(e, KERNEL).read_bytes()) == b"kernel 1"
    e.save(built, SNAP, {"elab_s": 1.0}, overwrite=True)
    assert gzip.decompress(stored_file(e, KERNEL).read_bytes()) == b"kernel 2"
    meta = json.loads(e.store.get(f"{e.key}/manifest.json"))["meta"]
    assert meta["elab_s"] == 1.0 and meta["bytes"] > meta["stored_bytes"] > 0 and "os" in meta["host"]


def test_save_without_a_snapshot_fails_and_stores_nothing(repo):
    e = elab(repo)
    write(repo / "build" / "a" / "build.log", "xelab failed")
    with pytest.raises(sim_cache.CacheError):
        e.save(repo / "build" / "a", SNAP, {})
    assert e.store.keys() == []


def test_mode_and_disabled_cache(repo, monkeypatch, capsys):
    assert elab_cache.mode() == "on"
    monkeypatch.setenv("SIM_ELAB_CACHE", "off")
    assert elab_cache.cached_elab("b", lambda: key_inputs(repo)) is None
    monkeypatch.setenv("SIM_ELAB_CACHE", "maybe")
    with pytest.raises(sim_cache.CacheError):
        elab_cache.mode()
    monkeypatch.setenv("SIM_ELAB_CACHE", "on")

    def broken():
        raise sim_cache.CacheError("XILINX_VIVADO/data/version.dat not found")

    assert elab_cache.cached_elab("b", broken) is None
    assert "disabled for b: XILINX_VIVADO" in capsys.readouterr().out


def test_cli(repo, capsys):
    e = saved(repo)
    assert sim_cache.main(["elab", "list"]) == 0
    out = capsys.readouterr().out
    assert e.key[:16] in out and "cfg/flow/vivado" in out
    assert sim_cache.main(["elab", "show", e.key[:8]]) == 0
    assert json.loads(capsys.readouterr().out)["key"] == e.key
    assert elab_cache.main(["diff", e.key, e.key]) == 0 and "same inputs" in capsys.readouterr().out


# ---- sim_runner.run() ----

class FakeRunner:
    """Stands in for the cocotb-vivado runner: counts builds and tests; a build writes a fake snapshot."""

    def __init__(self) -> None:
        self.builds = self.tests = 0
        self.build_kw = {}
        self.elab_args = []
        self.snapshot_name = ""

    def build(self, *, build_dir, log_file=None, **kw) -> None:
        self.builds += 1
        self.build_kw = kw
        write(Path(build_dir) / "xsim.dir" / kw["hdl_toplevel"] / "xsimk.so", f"kernel {self.builds}")
        if log_file is not None:
            Path(log_file).write_text("xelab: built\n")

    def test(self, *, build_dir, test_dir, log_file=None, seed=None, **kw) -> Path:
        self.tests += 1
        assert (Path(test_dir) / "xsim.dir" / kw["hdl_toplevel"] / "xsimk.so").is_file()
        if log_file is not None:
            Path(log_file).write_text("INFO test passed\n")
        (Path(test_dir) / "out.json").write_text("{}")
        xml = Path(test_dir) / "r.result.xml"
        xml.write_text('<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>')
        return xml


@pytest.fixture
def runner_env(repo, monkeypatch):
    pytest.importorskip("cocotb_tools")
    import sim_runner

    monkeypatch.setattr(sim_cache, "python_modules", lambda m: [{"path": f"{m}.py", "sha256": "0"}])
    src = write(repo / "rtl" / "top.sv", "module top; endmodule")

    def run(fake, sim_build: Path | None = None, **kw):
        monkeypatch.setattr(sim_runner, "SIM_BUILD", sim_build or repo / "build" / "sim")
        monkeypatch.setattr(sim_runner, "vivado_runner", lambda: fake)
        sim_runner.run(**{"sources": [src], "hdl_toplevel": SNAP, "test_module": "test_a", "build_name": "a",
                          **kw})
        return fake

    return run


def test_runner_reuses_then_restores_the_snapshot(runner_env, repo, capsys):
    run = runner_env
    first = run(FakeRunner())
    assert (first.builds, first.tests) == (1, 1) and "stored" in capsys.readouterr().out
    again = run(FakeRunner())                                   # the same build directory: its own snapshot
    assert (again.builds, again.tests, again.snapshot_name) == (0, 1, SNAP)
    assert "local snapshot" in capsys.readouterr().out
    elsewhere = run(FakeRunner(), sim_build=repo / "clone" / "build" / "sim")    # another checkout
    assert (elsewhere.builds, elsewhere.tests, elsewhere.snapshot_name) == (0, 1, SNAP)
    assert "hit in" in capsys.readouterr().out
    assert (repo / "clone" / "build" / "sim" / "a" / "vivado" / KERNEL).read_text() == "kernel 1"
    changed = run(FakeRunner(), verilog_defines={"X": "2"})     # another key: elaborated
    assert changed.builds == 1


def test_runner_with_the_result_cache(runner_env, repo, capsys):
    run = runner_env
    run(FakeRunner(), cache=True, cache_outputs=["out.json"])
    capsys.readouterr()
    for p in (repo / ".sim_cache").iterdir():               # only the result entries go: their run must run
        if p.name != "elab":
            shutil.rmtree(p)
    second = run(FakeRunner(), cache=True, cache_outputs=["out.json"])
    assert (second.builds, second.tests) == (0, 1)           # a result miss on a stored snapshot
    assert "local snapshot" in capsys.readouterr().out
    assert sim_cache.DirStore(repo / ".sim_cache").keys()     # the result is stored again


def test_runner_modes(runner_env, repo, monkeypatch, capsys):
    run = runner_env
    run(FakeRunner())
    monkeypatch.setenv("SIM_ELAB_CACHE", "refresh")
    refreshed = run(FakeRunner())
    assert refreshed.builds == 1
    monkeypatch.setenv("SIM_ELAB_CACHE", "off")
    off = run(FakeRunner())
    assert off.builds == 1 and "always" in off.build_kw
    monkeypatch.setenv("SIM_ELAB_CACHE", "on")
    monkeypatch.setenv("WAVES", "1")
    waves = run(FakeRunner())
    assert waves.builds == 1 and "elab_cache: bypassed: waves requested" in capsys.readouterr().out


def test_runner_store_failures_do_not_fail_the_test(runner_env, repo, monkeypatch, capsys):
    run = runner_env

    def broken(*a, **kw):
        raise sim_cache.CacheError("store exploded")

    monkeypatch.setattr(sim_cache.DirStore, "put", broken)
    first = run(FakeRunner())
    assert first.tests == 1 and "store failed: store exploded" in capsys.readouterr().out
    monkeypatch.setattr(sim_cache.DirStore, "get", broken)
    (repo / "build" / "sim" / "a" / "vivado" / elab_cache.STAMP).unlink()       # no local snapshot
    second = run(FakeRunner())
    assert (second.builds, second.tests) == (1, 1) and "lookup failed, elaborating" in capsys.readouterr().out


def test_runner_elaborates_when_the_snapshot_does_not_load(runner_env, repo, monkeypatch, capsys):
    run = runner_env
    run(FakeRunner())
    monkeypatch.setattr(elab_cache, "load_error", lambda run_dir, snapshot: "GLIBC_2.38 not found")
    other = run(FakeRunner(), sim_build=repo / "clone" / "build" / "sim")
    assert (other.builds, other.tests) == (1, 1) and "does not load on this host" in capsys.readouterr().out


# ---- The characterization's snapshots (characterize/bench/snapshots.py) ----

def test_snapshots_are_restored_and_stored(repo, capsys):
    import snapshots

    builds = []

    def build(root: Path):
        builds.append(root)
        fake_build(root)
        return None

    inputs = key_inputs(repo)
    root = repo / "characterize" / "snapshot_engine"
    stamp = snapshots.ensure(root, SNAP, inputs, build, elab=elab_cache.CachedElab("c", inputs))
    assert len(builds) == 1 and "dpi/libxfft_engine.so" in stamp["snapshot"]["files"]
    assert "dpi/cmodel/libgmp.so.11" in stamp["snapshot"]["files"]

    other = repo / "clone" / "characterize" / "snapshot_engine"
    restored = snapshots.ensure(other, SNAP, inputs, build, elab=elab_cache.CachedElab("c", inputs))
    assert len(builds) == 1 and "restored snapshot" in capsys.readouterr().out
    assert restored["snapshot"]["files"] == stamp["snapshot"]["files"] and restored["libraries"]["files"] == {}
    again = snapshots.ensure(other, SNAP, inputs, build, elab=elab_cache.CachedElab("c", inputs))
    assert len(builds) == 1 and again == restored                   # a restored snapshot is reused as built

    run_dir = repo / "clone" / "characterize" / "run_a"
    snapshots.prepare_run_dir(other, SNAP, restored, run_dir)
    assert (run_dir / "dpi" / "constants_source.txt").read_text() == "TABLE"
    assert (run_dir / "dpi" / "cmodel" / "libgmp.so.11").is_file() and not (run_dir / "ip").exists()
