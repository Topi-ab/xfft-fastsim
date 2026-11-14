"""The phases of the characterization bench: the cases on one model, the extraction of the constants and the
comparison of the two models. python -m characterize (characterize/api.py) runs them in this order: the cases on
the real core, the extraction, the same cases on the stand-in built from the extracted constants, the comparison.

Every case is one pytest case of test_characterize.py and one cached run, run as its own pytest process, at most
`jobs` processes at a time. Each model is built once per configuration (the first case that misses builds it,
under a lock); on a cache hit nothing is built. Every process gets the environment the caller passes (XFFT_CONFIG,
SIM_CACHE, SIM_CACHE_URL). The log of each case: build/characterize/<tag>/logs/<case>-<model>.log.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE)]
import cases  # noqa: E402


@dataclass(frozen=True)
class CaseRun:
    """One case on one model: whether it passed, whether the result came from the cache, its wall time, its log."""
    case: str
    model: str
    passed: bool
    hit: bool
    wall_s: float
    log: Path


def log_dir(tag: str) -> Path:
    return cases.BUILD / tag / "logs"


def run_case(tag: str, case: str, model: str, env: dict) -> CaseRun:
    """One pytest process: the case on the model."""
    logs = log_dir(tag)
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{case}-{model}.log"
    node = f"{HERE / 'test_characterize.py'}::test_characterize[{case}-{model}]"
    t0 = time.monotonic()
    with open(log, "w") as f:
        rc = subprocess.run([sys.executable, "-m", "pytest", node, "-q", "-s", "-p", "no:cacheprovider"],
                            cwd=REPO, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    text = log.read_text(errors="replace")
    return CaseRun(case, model, rc == 0, "sim_cache: hit" in text, time.monotonic() - t0, log)


def run_phase(tag: str, names: list[str], model: str, jobs: int, env: dict,
              say: Callable[[str], None] = print) -> list[CaseRun]:
    """The cases on one model, `jobs` at a time; one progress line per finished case. The results in the order of
    names."""
    t0 = time.monotonic()
    done: dict[str, CaseRun] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(run_case, tag, c, model, env) for c in names]
        for future in as_completed(futures):
            r = future.result()
            done[r.case] = r
            say(f"  [{len(done):3d}/{len(names)}] {r.case:26s} {model:6s} {'pass' if r.passed else 'FAIL'} "
                f"{'hit ' if r.hit else 'miss'} {r.wall_s:6.1f} s"
                + ("" if r.passed else f"   (log: {r.log.relative_to(REPO)})"))
    results = [done[c] for c in names]
    say(f"characterize: {model}: {len(results)} cases in {time.monotonic() - t0:.0f} s wall, {jobs} at a time, "
        f"{sum(r.hit for r in results)} cache hits, {sum(not r.passed for r in results)} failed")
    return results


def run_script(script: str, args: list[str], env: dict, say: Callable[[str], None] = print) -> tuple[int, list[str]]:
    """extract.py or compare.py of the bench: (exit code, output lines); the output is passed on line by line."""
    proc = subprocess.Popen([sys.executable, str(HERE / script), *args], cwd=REPO, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        say(line)
    return proc.wait(), lines
