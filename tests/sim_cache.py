"""Result cache of the cocotb runs (sim_runner.run(cache=True)): a content-addressed store of passing runs.

The key is the sha256 of a canonical JSON manifest of everything that decides a run's result, collected before
the build: the HDL sources (contents, in compile order), the IP builder Tcl, the stand-in's own files and
the vendor archive, the data files the test reads, the local Python modules the test imports (but for the caches'
own code, sim_cache.py and elab_cache.py, which decides no result), the run arguments
(toplevel, generics, test case, seed, the environment cocotb passes on) and the toolchain (simulator version,
Vivado's version.dat, its patch areas, Python and package versions). No timestamps, paths of the checkout or
host names, so equal inputs give the same key on any machine. The content is the run's result: cocotb's result
XML, the build and simulator logs and the files the test declares as outputs. A key that is found means the run
passed with exactly these inputs, so it is not run again; its log is printed and its files are restored into
the test directory instead. A run that misses is built from scratch or gets its snapshot from the elaboration
cache (elab_cache.py, keyed by every build input), so a stale build never lands under a new key.

Inputs the key cannot see are the caller's to declare (sim_runner.run(cache_inputs=...)): files a test or an
HDL source reads at run time. A test is only cached when it opts in, after that is checked.

Store: SIM_CACHE_URL, a directory (default <repo>/.sim_cache; a relative path is taken from the repository
root) or s3://<bucket>/<prefix> (boto3; endpoint, region and credentials from the usual AWS settings, the AWS_*
environment or ~/.aws, e.g. AWS_ENDPOINT_URL for an S3-compatible server). S3 permissions: s3:GetObject and
s3:PutObject on <prefix>/*, and s3:ListBucket on the bucket (without it S3 answers a missing key with 403,
which cannot be told from a denied one: every miss is then logged as a failed lookup, and the CLI cannot list).
Layout under the store root:
    <key>/manifest.json         inputs, metadata and the file list; written last, so it marks a complete entry
    <key>/files/<sha256>        result XML, logs, declared outputs, named by content; the manifest maps the
                                names (relative to the test directory) to them
    index/<test id>.json        the latest key of a test, to explain a miss (what changed since then)
Files named by content: when two runs that missed at the same time store the same key, the manifest that
lands last refers to its own files, which are all there, so the entry is complete whichever wins. An entry that
lookup() found damaged is overwritten by the next store.
SIM_CACHE: on (default: use a hit, store a pass), off (no lookup, no store), refresh (run and store).
The cache never fails a test: a store that cannot be read or written is logged and the test runs uncached.

S3 requests: a request that times out (read timeout 30 s), cannot connect or gets a server error (5xx, SlowDown)
is tried S3_ATTEMPTS times in all, with a growing random pause between (S3_BACKOFF_S); a missing key is a miss at
once, a denied request is not tried again. A lookup that still fails is a miss: the test runs.

CLI (python tests/sim_cache.py ...): list | show <key> | diff <key> <key> | elab list|show|diff (elab_cache.py)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from importlib import metadata
from modulefinder import ModuleFinder
from pathlib import Path, PurePosixPath

SCHEMA = 2       # 2: files stored by content (<key>/files/<sha256>)
REPO = Path(__file__).resolve().parents[1]
DEFAULT_URL = REPO / ".sim_cache"
# Seed of a cached run when COCOTB_RANDOM_SEED is not set: a random seed would make the key a lie
DEFAULT_SEED = 1
# Distributions of the cache backend and of its unit tests; they do not change a simulation result
BACKEND_DISTS = {"boto3", "botocore", "s3transfer", "jmespath", "urllib3", "python-dateutil", "six", "pip",
                 "setuptools", "wheel", "moto", "responses", "werkzeug", "xmltodict", "cryptography", "cffi",
                 "pycparser", "requests", "charset-normalizer", "idna", "certifi", "markupsafe", "pyyaml"}
# Version of each simulator
TOOL_VERSION_CMD = {"vivado": ["xelab", "--version"], "nvc": ["nvc", "--version"], "ghdl": ["ghdl", "--version"],
                    "verilator": ["verilator", "--version"], "riviera": ["vsimsa", "-version"]}
# Environment variables without the COCOTB_ prefix that cocotb, the GPI or Python read and that change a run
OTHER_ENV = ("GPI_USERS", "PYGPI_USERS", "GPI_EXTRA", "LIBPYTHON_LOC", "SIM_CMD_PREFIX", "SIM_CMD_SUFFIX", "GUI",
             "PYTHONOPTIMIZE", "PYTHONHASHSEED", "TOPLEVEL_LANG", "RANDOM_SEED")
MODES = ("on", "off", "refresh")
# The caches' own code: in the test's import closure, but it decides no result (a change must not miss every entry)
CACHE_CODE = ("sim_cache.py", "elab_cache.py")
# S3: attempts per request in all, the first pause between them (doubled each time, times 0.5 .. 1.5)
S3_ATTEMPTS = 3
S3_BACKOFF_S = 2.0
# Error codes of a missing key (GetObject, HeadObject) and of a server that is busy or failing (tried again)
S3_MISSING = {"NoSuchKey", "404", "NotFound"}
S3_TRANSIENT = {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "InternalError",
                "ServiceUnavailable", "500", "502", "503", "504"}


class CacheError(Exception):
    """The inputs cannot be fingerprinted, or the store cannot be reached."""


class CachedResult(UserWarning):
    """A test that passed from the cache, not run: pytest lists it in its warnings summary."""


def log(msg: str) -> None:
    print(f"sim_cache: {msg}", flush=True)


# ---- Fingerprints of the inputs ----

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def display_path(path: Path) -> str:
    """Path for the key: relative to the repository or to the Vivado installation, so not machine bound."""
    path = Path(path).resolve()
    roots = [("", REPO)]
    if os.environ.get("XILINX_VIVADO"):
        roots.append(("$XILINX_VIVADO/", Path(os.environ["XILINX_VIVADO"]).resolve()))
    for prefix, root in roots:
        if path.is_relative_to(root):
            return prefix + path.relative_to(root).as_posix()
    return path.as_posix()


def file_entry(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise CacheError(f"input {path} is missing")
    return {"path": display_path(path), "sha256": sha256_file(path)}


def files_entries(paths: Iterable[Path]) -> list[dict]:
    """Files, and the files below directories (sorted), in the given order."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += [file_entry(f) for f in sorted(p.rglob("*")) if f.is_file()]
        else:
            out.append(file_entry(p))
    return out


def tagged_types() -> tuple:
    try:
        from cocotb_tools.runner import VHDL, Verilog
    except ImportError:
        return ()
    return (VHDL, Verilog)


def source_entry(src) -> dict:
    """One runner source: a path, a VHDL()/Verilog()-tagged path, a DpiModel or a cocotb-vivado VivadoIp."""
    if isinstance(src, (str, Path)):
        return {"kind": "file", **file_entry(Path(src))}
    if isinstance(src, tagged_types()):
        return {"kind": type(src).__name__, **file_entry(Path(src.value))}
    if hasattr(src, "cache_entry"):
        return src.cache_entry(file_entry)            # sim_runner.DpiModel
    if type(src).__name__ == "VivadoIp":
        # The builder Tcl generates the .xci files: it is the input, the .xci names are identifiers
        # (cocotb-vivado's own fingerprint does the same, but with machine-bound absolute paths)
        if src.builder_tcl is None:
            ips = [file_entry(p) for p in src.paths]
        else:
            ips = [p.as_posix() for p in src.paths]
        part = getattr(src, "_part_num", None) or os.environ.get("COCOTB_DEFAULT_PART_NUM")
        return {"kind": "VivadoIp", "xci": ips, "part_num": part,
                "builder_tcl": None if src.builder_tcl is None else file_entry(src.builder_tcl)}
    raise CacheError(f"no fingerprint for source {src!r} ({type(src).__name__})")


def python_modules(test_module: str) -> list[dict]:
    """The test module and the repository's Python modules it imports, directly or not (static analysis).

    Imports from outside the repository (cocotb, numpy) are covered by the package versions. A module that is
    imported at run time by name (importlib) is not seen; declare its file in cache_inputs."""
    mod = sys.modules.get(test_module)
    file = Path(mod.__file__) if mod is not None and getattr(mod, "__file__", None) else None
    if file is None:
        from importlib.util import find_spec
        spec = find_spec(test_module)
        if spec is None or spec.origin is None:
            raise CacheError(f"test module {test_module} not found")
        file = Path(spec.origin)
    venvs = [Path(sys.prefix).resolve(), REPO / ".venv"]
    local = [p for p in dict.fromkeys(sys.path) if p and Path(p).resolve().is_relative_to(REPO)
             and not any(Path(p).resolve().is_relative_to(v) for v in venvs)]
    finder = ModuleFinder(path=local)
    finder.run_script(str(file))
    files = {Path(file).resolve()}
    files |= {Path(m.__file__).resolve() for m in finder.modules.values() if m.__file__}
    # pytest loads the conftest.py files above the test module before it
    files |= {c.resolve() for d in [file.parent, *file.parents] if d.is_relative_to(REPO)
              for c in [d / "conftest.py"] if c.is_file()}
    files -= {Path(__file__).resolve().with_name(n) for n in CACHE_CODE}
    return sorted((file_entry(f) for f in files), key=lambda e: e["path"])


@lru_cache
def tool_fingerprint(sim: str) -> dict:
    cmd = TOOL_VERSION_CMD.get(sim)
    if cmd is None:
        raise CacheError(f"no version command for simulator {sim}")
    try:
        # In a scratch directory: the XSim tools leave .pb files in their working directory
        with tempfile.TemporaryDirectory() as scratch:
            version = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120,
                                     cwd=scratch).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise CacheError(f"{' '.join(cmd)} failed: {e}") from e
    out = {"sim": sim, "version": version}
    if sim == "vivado":
        # xelab prints only "Vivado Simulator v<release>"; the installation's version.dat (encoded) tells builds
        # apart, the patch areas (the installation's patches/ and XILINX_PATH / MYVIVADO, which Vivado's
        # setupEnv.sh puts in front of it) tell patches apart
        vivado = Path(os.environ.get("XILINX_VIVADO", ""))
        if not (vivado / "data" / "version.dat").is_file():
            raise CacheError("XILINX_VIVADO/data/version.dat not found")
        out["version_dat"] = sha256_file(vivado / "data" / "version.dat")
        roots = [vivado / "patches"] + [Path(p) for v in ("XILINX_PATH", "MYVIVADO")
                                        for p in os.environ.get(v, "").split(":") if p]
        out["patches"] = files_entries(r for r in roots if r.exists())
    return out


@lru_cache
def python_fingerprint() -> dict:
    dists = {}
    for d in metadata.distributions():
        name = re.sub(r"[-_.]+", "-", (d.metadata["Name"] or "")).lower()
        if name and name not in BACKEND_DISTS:
            dists[name] = d.version
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "machine": platform.machine(), "packages": [f"{k}=={v}" for k, v in sorted(dists.items())]}


def cocotb_env(extra_env: Mapping[str, str]) -> dict:
    """The environment the test process gets as cocotb builds it (extra_env, then os.environ over it): the
    extra_env names, COCOTB_* (the seed has its own field) and OTHER_ENV."""
    eff = {**{k: str(v) for k, v in extra_env.items()}, **os.environ}
    names = set(extra_env) | {k for k in eff if k.startswith("COCOTB_")} | set(OTHER_ENV)
    names -= {"COCOTB_RANDOM_SEED", "RANDOM_SEED"}
    return {k: eff[k] for k in sorted(names) if k in eff}


def seed() -> int:
    value = os.environ.get("COCOTB_RANDOM_SEED") or os.environ.get("RANDOM_SEED") or DEFAULT_SEED
    try:
        return int(value)
    except ValueError as e:
        raise CacheError(f"COCOTB_RANDOM_SEED={value} is not an integer") from e


def canonical(value) -> str:
    def refuse(obj):
        raise CacheError(f"not a JSON value in the key: {obj!r} ({type(obj).__name__})")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=refuse)


def make_key(inputs: Mapping) -> str:
    return hashlib.sha256(canonical(inputs).encode()).hexdigest()


def safe_name(name: str) -> bool:
    """A stored file name: relative, inside the test directory."""
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and "\\" not in name


# ---- Stores ----

class DirStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def __str__(self) -> str:
        return str(self.root)

    def get(self, name: str) -> bytes | None:
        try:
            return (self.root / name).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            raise CacheError(f"{self.root / name}: {e}") from e

    def put(self, name: str, data: bytes) -> None:
        path = self.root / name
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)   # atomic: a reader sees the old or the new file, never half of one
        except OSError as e:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
            raise CacheError(f"{path}: {e}") from e

    def keys(self) -> list[str]:
        """Keys of the complete entries (with a manifest)."""
        try:
            return sorted(p.parent.name for p in self.root.glob("*/manifest.json"))
        except OSError as e:
            raise CacheError(f"{self.root}: {e}") from e


class S3Store:
    # A hung or unreachable endpoint must not hold a test for minutes. botocore tries each call once: the
    # attempts and pauses are this class's (call()). total_max_attempts counts the first attempt; botocore's
    # max_attempts does not (1 would be two).
    TIMEOUTS = {"connect_timeout": 5, "read_timeout": 30, "retries": {"mode": "standard", "total_max_attempts": 1}}

    def __init__(self, bucket: str, prefix: str) -> None:
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import (BotoCoreError, ClientError, ConnectionError, HTTPClientError,
                                             IncompleteReadError)
        except ImportError as e:
            raise CacheError("an s3:// store needs boto3: pip install -r requirements.txt") from e
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.errors = (BotoCoreError, ClientError)
        self.client_error = ClientError
        # Timeouts, refused or dropped connections, a body cut short: tried again
        self.transient = (HTTPClientError, ConnectionError, IncompleteReadError)
        try:
            self.client = boto3.client("s3", config=Config(**self.TIMEOUTS))
        except (BotoCoreError, ValueError) as e:
            raise CacheError(f"S3 client: {e}") from e

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    @staticmethod
    def error_code(e: Exception) -> str:
        """The S3 error code of a ClientError ("NoSuchKey", "404", ...); "" for any other error (a timeout or
        connection error has no response: botocore sets it to None)."""
        response = getattr(e, "response", None) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return code or (str(status) if status else "")

    def is_transient(self, e: Exception) -> bool:
        if isinstance(e, self.client_error):
            status = ((getattr(e, "response", None) or {}).get("ResponseMetadata") or {}).get("HTTPStatusCode")
            return self.error_code(e) in S3_TRANSIENT or (isinstance(status, int) and status >= 500)
        return isinstance(e, self.transient)

    def call(self, what: str, fn):
        """fn(); a transient error is tried again up to S3_ATTEMPTS in all, with a pause of
        S3_BACKOFF_S x 2^(attempt-1) x 0.5 .. 1.5 between. The last error, or one that is not transient, is
        raised as it is (the callers tell a missing key from a failure)."""
        for attempt in range(1, S3_ATTEMPTS + 1):
            try:
                return fn()
            except self.errors as e:
                if attempt == S3_ATTEMPTS or not self.is_transient(e):
                    raise
                pause = S3_BACKOFF_S * 2 ** (attempt - 1) * random.uniform(0.5, 1.5)
                log(f"S3 {what}: {type(e).__name__}: {e}; attempt {attempt} of {S3_ATTEMPTS}, again in "
                    f"{pause:.1f} s")
                time.sleep(pause)

    def failure(self, what: str, e: Exception) -> CacheError:
        code = self.error_code(e)
        hint = " (no s3:ListBucket? S3 then answers a missing key with 403)" if code in ("AccessDenied", "403") \
            else ""
        tries = f" after {S3_ATTEMPTS} attempts" if self.is_transient(e) else ""
        return CacheError(f"S3 {what}: {type(e).__name__}: {e}{tries}{hint}")

    def get(self, name: str) -> bytes | None:
        key = self.prefix + name
        try:
            # The body is read inside the attempt: a read that times out is tried again too
            return self.call(f"get {key}", lambda: self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except self.errors as e:
            if self.error_code(e) in S3_MISSING:
                return None
            raise self.failure(f"get {key}", e) from e

    def put(self, name: str, data: bytes) -> None:
        key = self.prefix + name
        try:
            self.call(f"put {key}", lambda: self.client.put_object(Bucket=self.bucket, Key=key, Body=data))
        except self.errors as e:
            raise self.failure(f"put {key}", e) from e

    def keys(self) -> list[str]:
        """Keys of the complete entries (with a manifest), as DirStore.keys()."""
        out = []
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                               Prefix=self.prefix):
                for obj in page.get("Contents", []):
                    parts = obj["Key"][len(self.prefix):].split("/")
                    if len(parts) == 2 and parts[1] == "manifest.json" and parts[0] != "index":
                        out.append(parts[0])
        except self.errors as e:
            raise self.failure("list", e) from e
        return sorted(out)


def open_store(url: str | None = None):
    url = url or os.environ.get("SIM_CACHE_URL") or str(DEFAULT_URL)
    if url.startswith("s3://"):
        bucket, _, prefix = url[len("s3://"):].partition("/")
        if not bucket:
            raise CacheError(f"no bucket in {url}")
        return S3Store(bucket, prefix)
    if url.startswith("file://"):
        url = url[len("file://"):]
    path = Path(url)
    return DirStore(path if path.is_absolute() else REPO / path)


def mode() -> str:
    m = os.environ.get("SIM_CACHE", "on").lower()
    if m not in MODES:
        raise CacheError(f"SIM_CACHE={m}: expected one of {', '.join(MODES)}")
    return m


# ---- Entries ----

def file_name(key: str, digest, suffix: str = "") -> str | None:
    """Where an entry's file is stored: by content, so that concurrent writers of one key never mix their files.
    None for a digest that is no sha256 (a damaged manifest: it must not name another object)."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return f"{key}/files/{digest}{suffix}"


def manifest_damaged(store, key: str) -> bool:
    """After read_manifest() returned None: whether a manifest is there (damaged) rather than missing."""
    try:
        return store.get(f"{key}/manifest.json") is not None
    except CacheError:
        return False


def read_manifest(store, key: str) -> dict | None:
    """The manifest of a complete, well-formed entry, else None (a damaged one is logged)."""
    data = store.get(f"{key}/manifest.json")
    if data is None:
        return None
    try:
        man = json.loads(data)
        ok = (man.get("key") == key and isinstance(man.get("files"), dict) and isinstance(man.get("meta"), dict)
              and isinstance(man.get("inputs"), dict))
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        log(f"entry {key[:16]} has a damaged manifest; ignored")
        return None
    return man


def flatten(value, path: str = "") -> dict:
    """Leaves of the inputs by path; lists of file entries are keyed by their path, other lists by position."""
    if isinstance(value, dict):
        return {k2: v2 for k, v in value.items() for k2, v2 in flatten(v, f"{path}.{k}" if path else k).items()}
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and "path" in v for v in value):
            return {k2: v2 for v in value for k2, v2 in flatten(v, f"{path}[{v['path']}]").items()}
        if value and all(isinstance(v, str) and "==" in v for v in value):     # package list
            return {f"{path}[{v.split('==')[0]}]": v.split("==", 1)[1] for v in value}
        return {k2: v2 for i, v in enumerate(value) for k2, v2 in flatten(v, f"{path}[{i}]").items()}
    return {path: value}


def diff(old: Mapping, new: Mapping) -> list[str]:
    a, b = flatten(old), flatten(new)
    out = []
    for k in sorted(a.keys() | b.keys()):
        if k not in b:
            out.append(f"- {k}")
        elif k not in a:
            out.append(f"+ {k}")
        elif a[k] != b[k]:
            out.append(f"~ {k}: {str(a[k])[:40]} -> {str(b[k])[:40]}")
    return out


def index_name(test_id: str) -> str:
    return "index/" + re.sub(r"[^A-Za-z0-9._=-]", "_", test_id) + ".json"


class CachedRun:
    """One cacheable run: its key, and the lookup / store against the configured store."""

    def __init__(self, test_id: str, inputs: Mapping, store=None) -> None:
        self.test_id = test_id
        # A frozen copy: the caller's dicts may change after the key is made
        self.inputs = json.loads(canonical({"schema": SCHEMA, **inputs}))
        self.key = make_key(self.inputs)
        self.store = store if store is not None else open_store()
        # Set by lookup() when the entry is there but damaged: the next save() replaces it
        self.damaged = False

    def lookup(self, test_dir: Path, clear: bool = False) -> bool:
        """True on a hit: the files are restored into test_dir and the stored log is printed. Never raises: a store
        that fails (after its retries) or a restore that fails is logged and is a miss, so the test runs.

        clear: empty test_dir first (only after the entry has been read and checked), so nothing of an earlier
        run is left next to the restored files for a later step to read."""
        try:
            return self.fetch(test_dir, clear)
        except Exception as e:      # the cache never fails a test
            log(f"lookup of {self.key[:16]} in {self.store} failed, running: {type(e).__name__}: {e}")
            return False

    def fetch(self, test_dir: Path, clear: bool) -> bool:
        man = read_manifest(self.store, self.key)
        if man is None:
            self.damaged = manifest_damaged(self.store, self.key)
            if not self.damaged:            # a damaged manifest is logged already
                self.explain_miss()
            return False
        bad = [n for n in man["files"] if not safe_name(n)]
        if bad:
            self.damaged = True
            log(f"entry {self.key[:16]} names files outside the test directory ({', '.join(bad)}); running")
            return False
        files = {}
        for name, digest in man["files"].items():
            where = file_name(self.key, digest)
            data = None if where is None else self.store.get(where)
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                self.damaged = True
                log(f"entry {self.key[:16]} is damaged ({name}); running")
                return False
            files[name] = data
        try:
            if clear and test_dir.exists():
                shutil.rmtree(test_dir)
            for name, data in files.items():
                (test_dir / name).parent.mkdir(parents=True, exist_ok=True)
                (test_dir / name).write_bytes(data)
        except OSError as e:
            raise CacheError(f"restore into {test_dir}: {e}") from e
        meta = man["meta"]
        summary = (f"{self.test_id} passed {meta.get('tests')} tests from the cache ({self.key[:16]}, stored "
                   f"{meta.get('stored')}, {meta.get('wall_s', 0):.0f} s); not run")
        log(f"hit in {self.store}: {summary}. Stored log:")
        if "sim.log" in files:
            sys.stdout.write(files["sim.log"].decode(errors="replace"))
        log(f"end of stored log ({self.key[:16]}); restored {', '.join(sorted(files))} into {test_dir}")
        warnings.warn(summary, CachedResult, stacklevel=4)
        return True

    def explain_miss(self) -> None:
        try:
            prev = self.store.get(index_name(self.test_id))
            old = read_manifest(self.store, json.loads(prev)["key"]) if prev else None
        except (ValueError, KeyError, TypeError, CacheError):
            old = None
        if old is None:
            log(f"miss {self.key[:16]}: no earlier entry of {self.test_id} in {self.store}")
            return
        changes = diff(old["inputs"], self.inputs)
        log(f"miss {self.key[:16]}: changed since {old['key'][:16]} ({old['meta'].get('stored')}): "
            + ("; ".join(changes[:20]) + (f"; ... {len(changes) - 20} more" if len(changes) > 20 else "")))

    def save(self, test_dir: Path, names: Sequence[str], meta: Mapping, overwrite: bool = False) -> None:
        """Store the files (names relative to test_dir) of a passed run, the manifest last.

        An existing complete entry is kept unless overwrite (SIM_CACHE=refresh) or lookup() found it damaged: two
        runs that missed at the same time store the same key, and their files (logs with times in them) differ.
        Each run's files are stored by content, so whichever manifest lands last names files that are there."""
        assert make_key(self.inputs) == self.key
        if self.damaged:
            log(f"replacing the damaged entry {self.key[:16]}")
        elif not overwrite and read_manifest(self.store, self.key) is not None:
            log(f"{self.key[:16]} already stored in {self.store}; kept")
            return
        digests = {}
        for name in names:
            if not safe_name(name):
                raise CacheError(f"output {name} is not inside the test directory")
            try:
                data = (test_dir / name).read_bytes()
            except OSError as e:
                raise CacheError(f"{test_dir / name}: {e}") from e
            digests[name] = hashlib.sha256(data).hexdigest()
            self.store.put(file_name(self.key, digests[name]), data)
        stored = datetime.now(timezone.utc).isoformat(timespec="seconds")
        man = {"key": self.key, "test_id": self.test_id, "inputs": self.inputs, "files": digests,
               "meta": {"stored": stored, **meta}}
        self.store.put(f"{self.key}/manifest.json", json.dumps(man, indent=1, sort_keys=True).encode())
        self.store.put(index_name(self.test_id), json.dumps({"key": self.key, "stored": stored}).encode())
        log(f"stored {self.key[:16]} in {self.store} ({', '.join(sorted(digests))})")


# ---- CLI ----

def main(argv: Sequence[str]) -> int:
    if argv[:1] == ["elab"]:
        import elab_cache
        return elab_cache.main(argv[1:])
    try:
        store = open_store()
        if argv[:1] == ["list"]:
            for key in store.keys():
                man = read_manifest(store, key)
                if man is not None:
                    print(f"{key[:16]}  {man['meta'].get('stored')}  {man['test_id']}  "
                          f"{man['meta'].get('tests')} tests")
            return 0
        if len(argv) == 2 and argv[0] == "show":
            print(json.dumps(read_manifest(store, resolve(store, argv[1])), indent=1, sort_keys=True))
            return 0
        if len(argv) == 3 and argv[0] == "diff":
            a, b = (read_manifest(store, resolve(store, k)) for k in argv[1:])
            print("\n".join(diff(a["inputs"], b["inputs"])) or "same inputs")
            return 0
    except CacheError as e:
        print(f"sim_cache: {e}", file=sys.stderr)
        return 1
    print(__doc__.split("CLI")[1].strip(), file=sys.stderr)
    return 1


def resolve(store, prefix: str) -> str:
    """The complete entry whose key starts with prefix."""
    keys = [k for k in store.keys() if k.startswith(prefix) and read_manifest(store, k) is not None]
    if len(keys) != 1:
        raise CacheError(f"{prefix}: {len(keys)} matching entries")
    return keys[0]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
