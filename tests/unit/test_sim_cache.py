"""Result cache of the cocotb runs (tests/sim_cache.py) and its use in sim_runner.run().

Runs without a simulator: the S3 store against moto, sim_runner with a stand-in for the cocotb-vivado runner.
    pytest tests/unit
"""

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

import sim_cache  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A stand-in repository: sim_cache sees tmp_path as the repository root."""
    monkeypatch.setattr(sim_cache, "REPO", tmp_path)
    for name in ("COCOTB_RANDOM_SEED", "SIM_CACHE", "SIM_CACHE_URL", "WAVES"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def inputs(src: Path, **extra) -> dict:
    return {"run": {"sim": "vivado", "parameters": {"N": 4}}, "sources": [sim_cache.source_entry(src)], **extra}


# ---- Key ----

def test_key_is_independent_of_dict_order():
    assert sim_cache.make_key({"a": 1, "b": {"c": 2, "d": 3}}) == sim_cache.make_key({"b": {"d": 3, "c": 2}, "a": 1})


def test_key_follows_contents_not_paths_or_times(repo):
    a = write(repo / "rtl" / "a.sv", "module a; endmodule")
    k1 = sim_cache.make_key(inputs(a))
    a.touch()                                   # new mtime, same contents
    assert sim_cache.make_key(inputs(a)) == k1
    assert inputs(a)["sources"][0]["path"] == "rtl/a.sv"
    write(a, "module a(); endmodule")
    assert sim_cache.make_key(inputs(a)) != k1


def test_key_follows_source_order(repo):
    a = write(repo / "a.sv", "a")
    b = write(repo / "b.sv", "b")
    ab = [sim_cache.source_entry(a), sim_cache.source_entry(b)]
    assert sim_cache.make_key({"sources": ab}) != sim_cache.make_key({"sources": ab[::-1]})


def test_tagged_sources(repo):
    runner = pytest.importorskip("cocotb_tools.runner")
    v = write(repo / "x.vhd", "x")
    assert sim_cache.source_entry(runner.VHDL(v)) == {"kind": "VHDL", "path": "x.vhd",
                                                      "sha256": sim_cache.sha256_file(v)}
    assert sim_cache.source_entry(runner.Verilog(v))["kind"] == "Verilog"


def test_vivado_ip_source_and_unknown_sources(repo):
    class VivadoIp:                             # cocotb_vivado.vivado.VivadoIp, the fields sim_cache reads
        def __init__(self, xci, builder_tcl, part):
            self.paths, self.builder_tcl, self._part_num = [Path(xci)], Path(builder_tcl), part

    tcl = write(repo / "build_ip.tcl", "create_ip")
    e = sim_cache.source_entry(VivadoIp("ip/core/core.xci", tcl, "xc7a35tcpg236-1"))
    assert e == {"kind": "VivadoIp", "xci": ["ip/core/core.xci"], "part_num": "xc7a35tcpg236-1",
                 "builder_tcl": {"path": "build_ip.tcl", "sha256": sim_cache.sha256_file(tcl)}}
    with pytest.raises(sim_cache.CacheError):
        sim_cache.source_entry(object())
    with pytest.raises(sim_cache.CacheError):
        sim_cache.source_entry(repo / "missing.sv")


def test_key_refuses_values_that_are_not_json():
    with pytest.raises(sim_cache.CacheError):
        sim_cache.make_key({"x": Path("a")})


def test_vivado_paths_are_relative_to_the_installation(repo, monkeypatch):
    vivado = repo.parent / f"{repo.name}_vivado"
    archive = write(vivado / "data" / "ip" / "core.zip", "zip")
    monkeypatch.setenv("XILINX_VIVADO", str(vivado))
    assert sim_cache.file_entry(archive)["path"] == "$XILINX_VIVADO/data/ip/core.zip"


def test_data_directories_are_expanded(repo):
    write(repo / "vec" / "b.txt", "2")
    write(repo / "vec" / "a" / "c.txt", "3")
    assert [e["path"] for e in sim_cache.files_entries([repo / "vec"])] == ["vec/a/c.txt", "vec/b.txt"]


def test_python_modules_follow_local_imports(repo, monkeypatch):
    sim = repo / "tests"
    write(sim / "conftest.py", "")
    write(sim / "helper.py", "import json\nfrom pkg import deep\n")
    write(sim / "pkg" / "__init__.py", "")
    write(sim / "pkg" / "deep.py", "import numpy\n")
    write(sim / "unrelated.py", "")
    write(sim / "test_x.py", """
        import helper

        def lazy():
            import pkg.deep
        """)
    monkeypatch.syspath_prepend(str(sim))
    paths = [e["path"] for e in sim_cache.python_modules("test_x")]
    assert "tests/test_x.py" in paths and "tests/helper.py" in paths and "tests/pkg/deep.py" in paths
    assert "tests/conftest.py" in paths
    assert "tests/unrelated.py" not in paths
    assert not any("numpy" in p for p in paths)


def test_seed_and_env(monkeypatch):
    for name in sim_cache.OTHER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COCOTB_RANDOM_SEED", raising=False)
    monkeypatch.delenv("COCOTB_TEST_FILTER", raising=False)
    assert sim_cache.seed() == sim_cache.DEFAULT_SEED
    monkeypatch.setenv("COCOTB_RANDOM_SEED", "77")
    monkeypatch.setenv("COCOTB_TEST_FILTER", "ideal")
    assert sim_cache.seed() == 77
    assert sim_cache.cocotb_env({"X": 1}) == {"COCOTB_TEST_FILTER": "ideal", "X": "1"}
    # cocotb puts os.environ over extra_env: the key must see the value the test gets
    monkeypatch.setenv("X", "from_shell")
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    assert sim_cache.cocotb_env({"X": 1}) == {"COCOTB_TEST_FILTER": "ideal", "PYTHONOPTIMIZE": "1",
                                              "X": "from_shell"}
    monkeypatch.setenv("COCOTB_RANDOM_SEED", "abc")
    with pytest.raises(sim_cache.CacheError):
        sim_cache.seed()


def test_python_fingerprint_leaves_out_backend_packages():
    names = {p.split("==")[0] for p in sim_cache.python_fingerprint()["packages"]}
    assert "pytest" in names
    assert not names & sim_cache.BACKEND_DISTS


def test_unknown_simulator_and_mode(monkeypatch):
    with pytest.raises(sim_cache.CacheError):
        sim_cache.tool_fingerprint("othersim")
    monkeypatch.setenv("SIM_CACHE", "maybe")
    with pytest.raises(sim_cache.CacheError):
        sim_cache.mode()


def test_diff_names_the_changed_leaves():
    old = {"sources": [{"path": "a.sv", "sha256": "1"}, {"path": "b.sv", "sha256": "2"}],
           "python": {"packages": ["cocotb==2.1.0", "numpy==2.5.3"]}, "seed": 1}
    new = {"sources": [{"path": "a.sv", "sha256": "9"}], "python": {"packages": ["cocotb==2.1.1", "numpy==2.5.3"]},
           "seed": 1}
    d = sim_cache.diff(old, new)
    assert "~ sources[a.sv].sha256: 1 -> 9" in d
    assert "- sources[b.sv].path" in d
    assert "~ python.packages[cocotb]: 2.1.0 -> 2.1.1" in d
    assert not any("seed" in line or "numpy" in line for line in d)


# ---- Stores and entries ----

def check_store_round_trip(store, repo: Path, capsys) -> None:
    src = write(repo / "a.sv", "a")
    run = sim_cache.CachedRun("t.a", inputs(src), store=store)
    out = repo / "build"
    assert not run.lookup(out)
    assert "no earlier entry" in capsys.readouterr().out

    write(out / "r.result.xml", "<testsuites/>")
    write(out / "sim.log", "latency 2182\n")
    write(out / "sub" / "res.json", "{}")
    run.save(out, ["r.result.xml", "sim.log", "sub/res.json"], {"tests": 4, "wall_s": 1.0})
    assert store.keys() == [run.key]

    restored = repo / "elsewhere"
    with pytest.warns(sim_cache.CachedResult):
        assert sim_cache.CachedRun("t.a", inputs(src), store=store).lookup(restored)
    assert "latency 2182" in capsys.readouterr().out
    assert (restored / "sub" / "res.json").read_text() == "{}"

    write(src, "a changed")
    miss = sim_cache.CachedRun("t.a", inputs(src), store=store)
    assert not miss.lookup(restored)
    out = capsys.readouterr().out
    assert f"changed since {run.key[:16]}" in out and "~ sources[a.sv].sha256" in out


def test_dir_store(repo, capsys):
    store = sim_cache.DirStore(repo / "cache")
    check_store_round_trip(store, repo, capsys)
    assert not list((repo / "cache").rglob("*.tmp"))


def stored_file(run: sim_cache.CachedRun, name: str) -> Path:
    """The file of a directory store's entry that the manifest maps name to."""
    digest = json.loads(run.store.get(f"{run.key}/manifest.json"))["files"][name]
    return run.store.root / sim_cache.file_name(run.key, digest)


def saved_run(repo: Path, test_id: str = "t") -> sim_cache.CachedRun:
    store = sim_cache.DirStore(repo / "cache")
    run = sim_cache.CachedRun(test_id, inputs(write(repo / "a.sv", "a")), store=store)
    write(repo / "b" / "r.xml", "x")
    run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0})
    return run


def test_damaged_entry_is_a_miss_and_does_not_clear_the_directory(repo, capsys):
    run = saved_run(repo)
    stored_file(run, "r.xml").write_text("y")
    keep = write(repo / "c" / "build" / "snapshot.so", "elaborated")
    assert not run.lookup(repo / "c", clear=True)
    assert "damaged" in capsys.readouterr().out
    assert keep.exists() and not (repo / "c" / "r.xml").exists()


def test_entry_without_manifest_is_a_miss(repo):
    """A store interrupted before the manifest leaves no entry."""
    store = sim_cache.DirStore(repo / "cache")
    run = sim_cache.CachedRun("t", inputs(write(repo / "a.sv", "a")), store=store)
    store.put(sim_cache.file_name(run.key, hashlib.sha256(b"x").hexdigest()), b"x")
    assert not run.lookup(repo / "c")
    assert store.keys() == []


def test_damaged_manifest_is_a_miss(repo, capsys):
    store = sim_cache.DirStore(repo / "cache")
    run = sim_cache.CachedRun("t", inputs(write(repo / "a.sv", "a")), store=store)
    store.put(f"{run.key}/manifest.json", b"{not json")
    store.put(sim_cache.index_name("t"), b"[]")
    assert not run.lookup(repo / "c")
    assert "damaged manifest" in capsys.readouterr().out


def test_stored_names_outside_the_test_directory_are_refused(repo, capsys):
    run = saved_run(repo)
    store = run.store
    man = json.loads(store.get(f"{run.key}/manifest.json"))
    man["files"]["../../escaped.txt"] = man["files"]["r.xml"]
    store.put(f"{run.key}/manifest.json", json.dumps(man).encode())
    assert not run.lookup(repo / "c" / "d")
    assert "outside the test directory" in capsys.readouterr().out
    assert not (repo / "escaped.txt").exists()
    with pytest.raises(sim_cache.CacheError):
        run.save(repo / "b", ["../a.sv"], {"tests": 1, "wall_s": 0}, overwrite=True)


def test_save_keeps_an_existing_entry_unless_overwrite(repo, capsys):
    run = saved_run(repo)
    write(repo / "b" / "r.xml", "second")
    run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0})
    assert "kept" in capsys.readouterr().out
    assert stored_file(run, "r.xml").read_bytes() == b"x"
    run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0}, overwrite=True)
    assert stored_file(run, "r.xml").read_bytes() == b"second"


def test_damaged_entry_is_replaced_by_the_next_pass(repo, capsys):
    """A damaged file under an intact manifest would otherwise miss for good: every later save kept it."""
    run = saved_run(repo)
    stored_file(run, "r.xml").write_text("y")
    again = sim_cache.CachedRun("t", run.inputs, store=run.store)
    assert again.key == run.key and not again.lookup(repo / "c")
    write(repo / "b" / "r.xml", "x")
    again.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0})
    assert "replacing the damaged entry" in capsys.readouterr().out
    assert sim_cache.CachedRun("t", run.inputs, store=run.store).lookup(repo / "d")


def test_manifest_digest_must_be_a_sha256(repo, capsys):
    run = saved_run(repo)
    man = json.loads(run.store.get(f"{run.key}/manifest.json"))
    man["files"]["r.xml"] = "../manifest.json"
    run.store.put(f"{run.key}/manifest.json", json.dumps(man).encode())
    assert not run.lookup(repo / "c")
    assert "is damaged (r.xml)" in capsys.readouterr().out


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes into a read-only directory")
def test_dir_store_errors_are_cache_errors(repo):
    root = repo / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        store = sim_cache.DirStore(root)
        with pytest.raises(sim_cache.CacheError):
            store.put("k/manifest.json", b"x")
        assert store.get("k/manifest.json") is None
        assert not list(root.rglob("*.tmp"))
    finally:
        root.chmod(0o700)


def test_inputs_are_frozen_at_the_key(repo):
    extra = {"always": False}
    run = sim_cache.CachedRun("t", {"run": {"build_extra": extra}}, store=sim_cache.DirStore(repo / "c"))
    extra["always"] = True
    assert run.inputs["run"]["build_extra"] == {"always": False}
    assert sim_cache.make_key(run.inputs) == run.key


def test_open_store(repo, monkeypatch):
    assert isinstance(sim_cache.open_store(), sim_cache.DirStore)
    assert sim_cache.open_store(f"file://{repo}/c").root == repo / "c"
    assert sim_cache.open_store("rel/cache").root == repo / "rel" / "cache"      # from the repository root
    monkeypatch.setenv("SIM_CACHE_URL", str(repo / "d"))
    assert sim_cache.open_store().root == repo / "d"
    with pytest.raises(sim_cache.CacheError):
        sim_cache.open_store("s3:///prefix")


def test_s3_store(repo, capsys, monkeypatch):
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
        store = sim_cache.open_store("s3://results/fft/sim")
        assert str(store) == "s3://results/fft/sim/"
        check_store_round_trip(store, repo, capsys)
        keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="results")["Contents"]]
        assert all(k.startswith("fft/sim/") for k in keys)
        assert any(k.startswith("fft/sim/index/") for k in keys)
        with pytest.raises(sim_cache.CacheError):
            sim_cache.open_store("s3://no-such-bucket/x").get("k")
        # keys() lists complete entries only, as DirStore does
        store.put("partial/files/r.xml", b"x")
        assert "partial" not in store.keys()


# ---- sim_runner.run() with a stand-in runner ----

class FakeRunner:
    """Stands in for the cocotb-vivado runner: counts builds and tests, writes the files a cocotb run leaves."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.builds = self.tests = 0
        self.seed = None
        self.build_kw = {}
        self.elab_args = []

    def build(self, *, build_dir, log_file=None, **kw) -> None:
        self.builds += 1
        self.build_kw = kw
        snap = Path(build_dir) / "xsim.dir" / kw["hdl_toplevel"]
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "xsimk.so").write_text(f"snapshot {self.builds}")
        if log_file is not None:
            Path(log_file).write_text("xelab: built\n")

    def test(self, *, build_dir, test_dir, log_file=None, seed=None, **kw) -> Path:
        self.tests += 1
        self.seed = seed
        test_dir = Path(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)
        if log_file is not None:
            Path(log_file).write_text("INFO ideal: first in -> first out 2182 clocks\n")
        (test_dir / "out.json").write_text('{"latency": 2182}')
        failures = int(self.fail)
        xml = test_dir / "r.result.xml"
        xml.write_text(f'<testsuites><testsuite tests="4" failures="{failures}" errors="0"/></testsuites>')
        if self.fail:
            sys.exit(1)                         # what cocotb's runner does under pytest
        return xml


@pytest.fixture
def runner_env(repo, monkeypatch):
    pytest.importorskip("cocotb_tools")
    import elab_cache
    import sim_runner

    for name in ("SIM_ELAB_CACHE", "SIM_ELAB_CACHE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(elab_cache, "load_error", lambda run_dir, snapshot: None)     # the fake snapshot "loads"
    monkeypatch.setattr(sim_runner, "SIM_BUILD", repo / "build" / "sim")
    monkeypatch.setenv("SIM_CACHE_URL", str(repo / "cache"))
    monkeypatch.setattr(sim_cache, "tool_fingerprint", lambda sim: {"sim": sim, "version": "fake 1.0"})
    monkeypatch.setattr(sim_cache, "python_modules", lambda m: [{"path": f"{m}.py", "sha256": "0"}])
    src = write(repo / "rtl" / "a.sv", "a")

    def run(fake, **kw):
        monkeypatch.setattr(sim_runner, "vivado_runner", lambda: fake)
        kw = {"sources": [src], "cache_outputs": ["out.json"], **kw}
        sim_runner.run(hdl_toplevel="a", test_module="test_a", build_name="a", cache=True, **kw)

    return sim_runner, run, src


def test_runner_stores_then_hits(runner_env, repo, capsys):
    sim_runner, run, src = runner_env
    first = FakeRunner()
    run(first)
    assert (first.builds, first.tests, first.seed) == (1, 1, sim_cache.DEFAULT_SEED)
    assert first.build_kw["always"] and not first.build_kw["clean"]     # from scratch, emptied before (dpi/ kept)
    out = capsys.readouterr().out
    assert "first out 2182" in out and "xelab: built" in out        # the logs are echoed while they run
    assert "stored" in out
    key = sim_cache.DirStore(repo / "cache").keys()[0]
    files = json.loads((repo / "cache" / key / "manifest.json").read_text())["files"]
    assert sorted(files) == ["build.log", "out.json", "r.result.xml", "sim.log"]

    build_dir = repo / "build" / "sim" / "a" / "vivado"
    (build_dir / "out.json").unlink()
    (build_dir / "stale_from_an_earlier_run.json").write_text("{}")
    second = FakeRunner()
    with pytest.warns(sim_cache.CachedResult, match="passed 4 tests from the cache"):
        run(second)
    assert (second.builds, second.tests) == (0, 0)
    assert "hit" in capsys.readouterr().out
    assert json.loads((build_dir / "out.json").read_text()) == {"latency": 2182}
    # Only the stored files are left in the build directory
    assert sorted(p.name for p in build_dir.iterdir()) == ["build.log", "out.json", "r.result.xml", "sim.log"]

    third = FakeRunner()
    run(third, parameters={"N": 8})              # another generic: another key
    assert third.tests == 1


def test_runner_modes(runner_env, monkeypatch, capsys):
    sim_runner, run, src = runner_env
    run(FakeRunner())
    monkeypatch.setenv("SIM_CACHE", "refresh")
    again = FakeRunner()
    run(again)
    assert again.tests == 1
    monkeypatch.setenv("SIM_CACHE", "off")
    off = FakeRunner()
    run(off)
    assert off.tests == 1
    monkeypatch.setenv("SIM_CACHE", "on")
    monkeypatch.setenv("WAVES", "1")
    waves = FakeRunner()
    run(waves)
    assert waves.tests == 1 and "bypassed" in capsys.readouterr().out


def test_runner_does_not_store_a_failure(runner_env, repo):
    sim_runner, run, src = runner_env
    with pytest.raises(SystemExit):
        run(FakeRunner(fail=True))
    assert sim_cache.DirStore(repo / "cache").keys() == []
    ok = FakeRunner()
    run(ok)
    assert ok.tests == 1


def test_runner_does_not_store_without_declared_outputs(runner_env, repo, capsys):
    sim_runner, run, src = runner_env
    run(FakeRunner(), cache_outputs=["missing.json"])
    assert "declared outputs missing" in capsys.readouterr().out
    assert sim_cache.DirStore(repo / "cache").keys() == []


def test_runner_store_failure_does_not_fail_the_test(runner_env, monkeypatch, capsys):
    sim_runner, run, src = runner_env

    def broken(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(sim_cache.CachedRun, "save", broken)
    ok = FakeRunner()
    run(ok)                                     # passes despite the store
    assert ok.tests == 1 and "store failed: store exploded" in capsys.readouterr().out
    monkeypatch.setattr(sim_cache.CachedRun, "lookup", broken)
    again = FakeRunner()
    run(again)
    assert again.tests == 1 and "lookup failed, running" in capsys.readouterr().out


def test_runner_uncached_without_opt_in(runner_env, repo, monkeypatch):
    sim_runner, run, src = runner_env
    fake = FakeRunner()
    monkeypatch.setattr(sim_runner, "vivado_runner", lambda: fake)
    sim_runner.run(sources=[src], hdl_toplevel="a", test_module="test_a", build_name="a")
    assert fake.tests == 1 and not fake.build_kw.get("clean") and fake.seed is None
    assert sim_cache.DirStore(repo / "cache").keys() == []


@pytest.mark.parametrize("value", ["1", "yes", "TRUE", "on"])
def test_runner_waves_values_bypass(runner_env, monkeypatch, capsys, value):
    sim_runner, run, src = runner_env
    monkeypatch.setenv("WAVES", value)
    fake = FakeRunner()
    run(fake)
    assert fake.tests == 1 and "bypassed" in capsys.readouterr().out


def test_key_is_independent_of_hash_seed_and_cwd(tmp_path):
    """Sets and directory listings have no stable order (PYTHONHASHSEED, file system): the fingerprints sort."""
    model = TESTS.parent / "model"
    script = textwrap.dedent(f"""
        import os, sys
        os.environ.pop("PYTHONHASHSEED")    # in effect for this interpreter; a set value is part of the key
        sys.path[:0] = [{str(TESTS)!r}, {str(model)!r}]
        import sim_cache
        print(sim_cache.make_key({{
            "py": sim_cache.python_modules("sim_cache"),
            "dir": sim_cache.files_entries([{str(model)!r}]),
            "python": sim_cache.python_fingerprint(),
            "env": sim_cache.cocotb_env({{"B": 1, "A": 2}}),
        }}))
        """)
    keys = {subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True,
                           cwd=cwd, env={"PYTHONHASHSEED": str(seed), "PATH": "/usr/bin:/bin"}).stdout
            for seed, cwd in ((1, TESTS.parent), (2, TESTS.parent), (3, tmp_path))}
    assert len(keys) == 1 and len(next(iter(keys)).strip()) == 64


# ---- DPI-C stand-in models (sim_runner.DpiModel) ----

def dpi_model(repo: Path):
    import sim_runner

    build = write(repo / "model" / "build.py", """
        import sys
        from pathlib import Path
        out = Path(sys.argv[1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "pkg.sv").write_text("package p; endpackage")
        (out / "libm.so").write_text("lib")
        """)
    sv = write(repo / "model" / "core.sv", "module core; endmodule")
    c = write(repo / "model" / "shim.c", "int x;")
    return sim_runner.DpiModel(name="m", build=build, sources=("pkg.sv", sv), inputs=(c, sv))


def test_dpi_model_fingerprint(runner_env, repo):
    model = dpi_model(repo)
    e = sim_cache.source_entry(model)
    assert e["kind"] == "DpiModel" and e["sources"][0] == "pkg.sv" and e["sources"][1]["path"] == "model/core.sv"
    k = sim_cache.make_key({"s": [e]})
    write(repo / "model" / "shim.c", "int y;")
    assert sim_cache.make_key({"s": [sim_cache.source_entry(model)]}) != k


def test_dpi_model_in_a_run(runner_env, repo):
    sim_runner, run, src = runner_env
    model = dpi_model(repo)
    fake = FakeRunner()
    seen = {}

    def build(*, sources, build_dir, **kw):
        seen["sources"], seen["build_dir"] = list(sources), Path(build_dir)
        return FakeRunner.build(fake, build_dir=build_dir, **kw)

    fake.build = build
    run(fake, sources=[model, src])
    build_dir = repo / "build" / "sim" / "a" / "vivado_m"                        # apart from the real core's
    dpi_dir = build_dir / "dpi"                                                 # inside it: relocatable
    assert seen["sources"] == [dpi_dir / "pkg.sv", repo / "model" / "core.sv", src]
    assert seen["build_dir"] == build_dir
    assert fake.elab_args == ["-sv_root", str(dpi_dir), "-sv_lib", "libm"]


# ---- The caches' own code is not in the key ----

def test_cache_code_is_not_in_the_key():
    """sim_cache.py and elab_cache.py decide no result: a change to them must not miss every entry."""
    names = {e["path"] for e in sim_cache.python_modules("sim_runner")}
    assert "tests/sim_runner.py" in names
    assert not names & {"tests/sim_cache.py", "tests/elab_cache.py"}


def test_a_comment_in_the_cache_code_keeps_the_key(tmp_path, monkeypatch):
    """A copy of tests/ as the repository: the key of a test module that imports sim_runner (and so sim_cache
    and elab_cache) stays when the cache code changes, and changes when sim_runner.py does."""
    import importlib
    tests = tmp_path / "tests"
    for name in ("sim_runner.py", "sim_cache.py", "elab_cache.py"):
        write(tests / name, (TESTS / name).read_text())
    write(tests / "test_keyed.py", "import sim_runner\n")
    monkeypatch.setattr(sim_cache, "REPO", tmp_path)
    monkeypatch.setattr(sim_cache, "__file__", str(tests / "sim_cache.py"))     # the copy is the cache code
    monkeypatch.syspath_prepend(str(tests))
    importlib.invalidate_caches()

    def key() -> str:
        return sim_cache.make_key({"python": sim_cache.python_modules("test_keyed")})

    before = key()
    assert {e["path"] for e in sim_cache.python_modules("test_keyed")} == {"tests/sim_runner.py",
                                                                           "tests/test_keyed.py"}
    for name in ("sim_cache.py", "elab_cache.py"):
        with open(tests / name, "a") as f:
            f.write("# a comment\n")
        assert key() == before, name
    with open(tests / "sim_runner.py", "a") as f:
        f.write("# a comment\n")
    assert key() != before


# ---- S3 failures: retried, then a miss, never an error of the test ----

class FakeS3:
    """Stands in for the boto3 client: each call pops the next outcome (an exception to raise, or a value)."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def answer(self, op: str, **kw):
        self.calls.append((op, kw.get("Key")))
        outcome = self.outcomes.pop(0) if self.outcomes else b"data"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_object(self, **kw):
        data = self.answer("get", **kw)

        class Body:
            def read(self):
                return data
        return {"Body": Body()}

    def put_object(self, **kw):
        return self.answer("put", **kw)


def client_error(code: str, status: int):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "GetObject")


@pytest.fixture
def s3(monkeypatch):
    """An S3Store whose client is a FakeS3 (set .client.outcomes); no pauses between attempts."""
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    monkeypatch.setattr(sim_cache, "S3_BACKOFF_S", 0.0)
    store = sim_cache.S3Store("results", "fft")
    store.client = FakeS3()
    return store


def test_s3_read_timeout_is_tried_again(s3, capsys):
    """An overloaded server answers with ReadTimeoutError, whose response is None (not a dict)."""
    from botocore.exceptions import ReadTimeoutError
    timeout = ReadTimeoutError(endpoint_url="https://s3.example/results/fft/k")
    assert getattr(timeout, "response", None) is None
    s3.client.outcomes = [timeout, timeout, b"third time"]
    assert s3.get("k/manifest.json") == b"third time"
    assert len(s3.client.calls) == sim_cache.S3_ATTEMPTS == 3
    assert "attempt 2 of 3" in capsys.readouterr().out


def test_s3_errors_after_the_last_attempt_are_cache_errors(s3):
    from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
    s3.client.outcomes = [ReadTimeoutError(endpoint_url="u")] * 3
    with pytest.raises(sim_cache.CacheError, match="ReadTimeoutError.*after 3 attempts"):
        s3.get("k")
    s3.client.outcomes = [EndpointConnectionError(endpoint_url="u")] * 3
    with pytest.raises(sim_cache.CacheError):
        s3.put("k", b"x")


def test_s3_server_errors_are_tried_again_and_denials_are_not(s3):
    s3.client.outcomes = [client_error("SlowDown", 503), client_error("InternalError", 500), b"ok"]
    assert s3.get("k") == b"ok" and len(s3.client.calls) == 3
    s3.client.calls, s3.client.outcomes = [], [client_error("AccessDenied", 403)]
    with pytest.raises(sim_cache.CacheError, match="ListBucket"):
        s3.get("k")
    assert len(s3.client.calls) == 1


def test_s3_missing_key_is_a_miss_at_once(s3):
    s3.client.outcomes = [client_error("NoSuchKey", 404)]
    assert s3.get("k") is None and len(s3.client.calls) == 1
    s3.client.outcomes = [client_error("404", 404)]
    assert s3.get("k") is None and len(s3.client.calls) == 2


def test_lookup_against_a_failing_store_is_a_miss(repo, s3, capsys):
    """The cache never fails a test: a lookup that fails after its retries is a miss (the test runs)."""
    from botocore.exceptions import ReadTimeoutError
    run = sim_cache.CachedRun("t", inputs(write(repo / "a.sv", "a")), store=s3)
    s3.client.outcomes = [ReadTimeoutError(endpoint_url="u")] * 10
    assert run.lookup(repo / "b", clear=True) is False
    assert "failed, running" in capsys.readouterr().out
    # An unexpected error inside the lookup is a miss as well
    s3.client.outcomes = [AttributeError("'NoneType' object has no attribute 'get'")]
    assert run.lookup(repo / "b") is False


def test_s3_timeouts_against_moto(repo, monkeypatch, capsys):
    """Timeouts injected into botocore's request path (before-send), against moto: a lookup whose requests time
    out twice each still hits; one whose requests always time out is a miss, and a store that times out fails
    with a CacheError (sim_runner logs it), never with anything else."""
    moto = pytest.importorskip("moto")
    import boto3
    from botocore.exceptions import ReadTimeoutError

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(sim_cache, "S3_BACKOFF_S", 0.0)
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="results",
                                         CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        store = sim_cache.open_store("s3://results/fft")
        run = sim_cache.CachedRun("t", inputs(write(repo / "a.sv", "a")), store=store)
        write(repo / "b" / "r.xml", "x")
        run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0})
        timeouts = {"per_request": 2, "seen": {}}

        def inject(request, **kw):
            n = timeouts["seen"].get(request.url, 0)
            timeouts["seen"][request.url] = n + 1
            if n < timeouts["per_request"]:
                raise ReadTimeoutError(endpoint_url=request.url)

        store.client.meta.events.register("before-send.s3", inject)
        with pytest.warns(sim_cache.CachedResult):
            assert run.lookup(repo / "c", clear=True) and (repo / "c" / "r.xml").read_text() == "x"
        assert set(timeouts["seen"].values()) == {3}            # two timeouts, then the answer
        timeouts["per_request"], timeouts["seen"] = 10**6, {}
        assert run.lookup(repo / "d") is False
        assert "ReadTimeoutError" in capsys.readouterr().out
        with pytest.raises(sim_cache.CacheError, match="after 3 attempts"):
            run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0}, overwrite=True)


def test_s3_backoff_grows_between_attempts(s3, monkeypatch):
    """Pauses of S3_BACKOFF_S x 2^(attempt-1) x 0.5 .. 1.5 between the attempts, none after the last."""
    from botocore.exceptions import ReadTimeoutError
    pauses = []
    monkeypatch.setattr(sim_cache, "S3_BACKOFF_S", 2.0)
    monkeypatch.setattr(sim_cache.time, "sleep", pauses.append)
    for _ in range(20):
        pauses.clear()
        s3.client.outcomes = [ReadTimeoutError(endpoint_url="u")] * 2 + [b"ok"]
        assert s3.get("k") == b"ok"
        assert len(pauses) == 2 and 1.0 <= pauses[0] <= 3.0 and 2.0 <= pauses[1] <= 6.0
    pauses.clear()
    s3.client.outcomes = [ReadTimeoutError(endpoint_url="u")] * 3
    with pytest.raises(sim_cache.CacheError):
        s3.get("k")
    assert len(pauses) == 2


# ---- Against a local S3 server that holds requests past the read timeout (fake_s3.py): botocore's own timeout ----

@pytest.fixture
def slow_s3(monkeypatch):
    """A FakeS3Server, the S3 stores of this test pointed at it, a read timeout of 0.4 s, no pauses."""
    pytest.importorskip("boto3")
    from fake_s3 import FakeS3Server
    server = FakeS3Server(hold_s=1.0).start()
    for name, value in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                        "AWS_DEFAULT_REGION": "eu-north-1", "AWS_ENDPOINT_URL": server.url,
                        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(sim_cache.S3Store, "TIMEOUTS", {**sim_cache.S3Store.TIMEOUTS, "read_timeout": 0.4})
    monkeypatch.setattr(sim_cache, "S3_BACKOFF_S", 0.0)
    yield server
    server.stop()


def hold(server, first: int) -> None:
    """From now on the first `first` requests of every object are held."""
    server.seen, server.hold_first = {}, first


def test_lookup_times_out_then_hits_on_a_slow_server(repo, slow_s3, capsys):
    """Each request times out once (a real ReadTimeoutError of botocore, response None), the next attempt answers:
    still a hit. Three timeouts each: a miss (the test runs), and the save fails as a CacheError."""
    store = sim_cache.open_store("s3://results/fft")
    assert store.client.meta.config.retries["total_max_attempts"] == 1    # the attempts are S3Store.call()'s
    run = sim_cache.CachedRun("t", inputs(write(repo / "a.sv", "a")), store=store)
    write(repo / "b" / "r.xml", "x")
    run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0})
    assert sim_cache.read_manifest(store, run.key) is not None
    capsys.readouterr()

    hold(slow_s3, 1)
    with pytest.warns(sim_cache.CachedResult):
        assert run.lookup(repo / "c", clear=True) and (repo / "c" / "r.xml").read_text() == "x"
    out = capsys.readouterr().out
    assert "ReadTimeoutError" in out and "attempt 1 of 3" in out and "attempt 2 of 3" not in out
    assert slow_s3.held >= 2                                     # the manifest and the file, each once

    hold(slow_s3, 3)
    assert run.lookup(repo / "d") is False
    assert "after 3 attempts" in capsys.readouterr().out
    manifest = f"/results/fft/{run.key}/manifest.json"
    assert [h for m, p, h in slow_s3.log if p == manifest][-3:] == [True] * 3     # botocore's own: once each
    hold(slow_s3, 3)
    with pytest.raises(sim_cache.CacheError, match="after 3 attempts"):
        run.save(repo / "b", ["r.xml"], {"tests": 1, "wall_s": 0}, overwrite=True)
    hold(slow_s3, 0)
    assert store.get("nothing") is None                          # 404: a miss


def test_runner_runs_the_test_when_lookups_time_out(runner_env, repo, slow_s3, monkeypatch, capsys):
    """sim_runner.run() against a server whose every request times out three times (the result and the
    elaboration lookups, and the stores after the run): the test runs and passes, the cache errors are logged."""
    sim_runner, run, src = runner_env
    monkeypatch.setenv("SIM_CACHE_URL", "s3://results/fft")
    first = FakeRunner()
    run(first)                                  # a working server: stored
    assert first.tests == 1 and "stored" in capsys.readouterr().out
    hold(slow_s3, 3)
    again = FakeRunner()
    run(again)
    out = capsys.readouterr().out
    assert again.tests == 1 and "ReadTimeoutError" in out and "failed, running" in out
    hold(slow_s3, 1)
    hit = FakeRunner()
    with pytest.warns(sim_cache.CachedResult):
        run(hit)                                # one timeout per request: still a hit
    assert hit.tests == 0
