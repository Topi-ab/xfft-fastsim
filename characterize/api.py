"""Characterization of an xfft core configuration, as a Python function: generate the core with Vivado, run the
bench's cases on it in XSim, extract the timing constants, run the same cases on the stand-in (the C engine, in
XSim) built from them and compare the two (docs/CHARACTERIZATION.md).

    import sys; sys.path.insert(0, "<repository root>")
    from characterize import characterize

    result = characterize("nfft=8", jobs=4)                   # or an XfftConfig, a dict, or xci="path/to.xci"
    result.ok, result.exit_code                               # True / 0 when the constants came out clean
    result.constants                                          # {"LATENCY_FIRST_OUT": ..., ...}
    result.problems                                           # [] or what the extraction could not explain
    result.json_path                                          # the JSON written (out=..., else the bench's own)

python -m characterize is a thin command line over characterize() (characterize/cli.py).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cases as bench_cases
import run
import sim_cache
import xfft_config
from . import parts
from . import xci as xci_reader

MODELS = ("rtl", "engine")
# Earlier names of the models, still accepted
MODEL_ALIASES = {"cmodel": "engine"}
# Exit codes of python -m characterize (Result.exit_code)
EXIT_OK = 0             # constants extracted without problems (and, when run, the comparison passed)
EXIT_PROBLEMS = 1       # the extraction has problems, or real-core cases failed: no clean constants
EXIT_USAGE = 2          # bad arguments or configuration, a Versal part, unsupported .xci, Vivado not found
EXIT_COMPARE = 3        # constants extracted without problems, but the stand-in differs from the core
SMALLEST_N = 256        # below this the bench's case positions give no evidence for some constants


class CharacterizeError(Exception):
    """The characterization cannot start: a bad configuration or argument, a Versal part, an unsupported .xci, no
    Vivado."""


@dataclass
class Result:
    """The outcome of characterize()."""
    config: xfft_config.XfftConfig
    constants: dict[str, int] | None    # None when the extraction wrote no JSON
    problems: list[str]                 # extraction problems and failed real-core cases
    json_path: Path | None              # the JSON of this run (a copy at out=..., else the bench's out/<tag>.json)
    compared: bool | None               # whether the stand-in matched the core (None: not run)
    comparison: list[str] = field(default_factory=list)    # the comparison's failures and failed stand-in cases
    warnings: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK

    @property
    def tag(self) -> str:
        return self.config.tag

    @property
    def ok(self) -> bool:
        """Constants extracted without problems (and, when compared, the stand-in matched)."""
        return self.exit_code == EXIT_OK

    @property
    def document(self) -> dict | None:
        """The JSON as a dict (config, tag, core_version, constants, evidence, relations, provenance[, problems])."""
        return json.loads(self.json_path.read_text()) if self.json_path else None


def resolve_config(config=None) -> xfft_config.XfftConfig:
    """An XfftConfig from an XfftConfig, an XFFT_CONFIG string ("nfft=8,rounding=truncation"), a dict of fields,
    or None (the default configuration)."""
    try:
        if config is None:
            return xfft_config.XfftConfig()
        if isinstance(config, xfft_config.XfftConfig):
            return config
        if isinstance(config, str):
            return xfft_config.parse(config)
        if isinstance(config, dict):
            return xfft_config.XfftConfig(**config)
    except (TypeError, ValueError) as e:
        raise CharacterizeError(f"configuration: {e}") from None
    raise CharacterizeError(f"configuration: {config!r}: expected an XfftConfig, a str or a dict")


def check_part(cfg: xfft_config.XfftConfig) -> None:
    """CharacterizeError for a Versal part (characterize/parts.py: by the part name; the .xci reader asks Vivado)."""
    if parts.is_versal(cfg.part):
        raise CharacterizeError(parts.versal_refusal(cfg.part))


def case_names(cfg: xfft_config.XfftConfig, names=None, extra: bool = False) -> list[str]:
    """The cases of a run: the named ones (checked), else the pinning cases and known approximations, with extra
    also the extra comparison cases (characterize/bench/cases.py)."""
    every = bench_cases.cases(cfg.points, extra=True)
    if names:
        unknown = [c for c in names if c not in every]
        if unknown:
            raise CharacterizeError(f"unknown cases: {' '.join(unknown)}")
        return list(names)
    return list(bench_cases.cases(cfg.points, extra=extra))


def out_path(out, tag: str) -> Path:
    """Where the JSON goes for out: a .json file, or a directory (<out>/<tag>.json)."""
    out = Path(out)
    return out if out.suffix == ".json" else out / f"{tag}.json"


def characterize(config=None, *, xci: str | os.PathLike | None = None, allow_differences: bool = False,
                 models=MODELS, jobs: int = 2, engine_jobs: int = 6, cases=None, extra: bool = False,
                 cache: str = "on", cache_url: str | None = None, elab_cache: str | None = None,
                 elab_cache_url: str | None = None, out: str | os.PathLike | None = None,
                 log: Callable[[str], None] | None = print, cmodel_jobs: int | None = None) -> Result:
    """Characterize one configuration end to end and return the Result.

    config: the configuration (resolve_config()); or xci: an existing .xci, read with Vivado (characterize/xci.py:
    unsupported options raise CharacterizeError, and so do differences between the IP and the core the bench
    generates, unless allow_differences). A Versal part raises CharacterizeError (characterize/parts.py), before
    anything is generated. models: "rtl" (the real core and the extraction only) and/or "engine" (the stand-in
    built from the extracted constants, and the comparison; "cmodel" is accepted for it). jobs / engine_jobs:
    simulations at a time on the real core / the stand-in (one CPU core each; cmodel_jobs is accepted for
    engine_jobs). cases: only these cases (default: all; the
    extraction needs all pinning cases). extra: also the extra comparison cases. cache: the result cache's mode
    (on, off, refresh); cache_url: its store (a directory or s3://bucket/prefix; default: SIM_CACHE_URL, else
    .sim_cache/ in the repository; docs/CACHE.md). elab_cache: the elaboration cache's mode (on, off, refresh;
    default: SIM_ELAB_CACHE, else on); elab_cache_url: its store (default: SIM_ELAB_CACHE_URL, else the result
    cache's). out: also write the JSON there (a .json file or a directory).
    log: progress lines (None: quiet).

    Raises CharacterizeError when it cannot start; everything after that is in the Result."""
    say = log or (lambda line: None)
    if xci is not None and config is not None:
        raise CharacterizeError("give a configuration or an .xci, not both")
    models = [models] if isinstance(models, str) else list(models)
    models = list(dict.fromkeys(MODEL_ALIASES.get(m, m) for m in models))
    if not models or set(models) - set(MODELS):
        raise CharacterizeError(f"models: {models}: rtl and/or engine")
    if cmodel_jobs is not None:
        engine_jobs = cmodel_jobs
    if cache not in sim_cache.MODES:
        raise CharacterizeError(f"cache: {cache}: one of {', '.join(sim_cache.MODES)}")
    if elab_cache is not None and elab_cache not in sim_cache.MODES:
        raise CharacterizeError(f"elab_cache: {elab_cache}: one of {', '.join(sim_cache.MODES)}")
    if xci is None:
        cfg = resolve_config(config)
        check_part(cfg)
    for tool in ("vivado", "xvlog", "xelab", "xsim"):
        if shutil.which(tool) is None:
            raise CharacterizeError(f"{tool} is not on PATH: source <Vivado>/settings64.sh")
    warnings = []
    if xci is not None:
        try:
            report = xci_reader.read_config(Path(xci), log=say)
        except xci_reader.XciError as e:
            raise CharacterizeError(str(e)) from None
        if report.unsupported:
            raise CharacterizeError(f"{xci}: options the stand-in does not support:\n  "
                                    + "\n  ".join(report.unsupported))
        if report.differences and not allow_differences:
            raise CharacterizeError(
                f"{xci}: the core the bench generates for {report.config.tag} differs from this IP in:\n  "
                + "\n  ".join(report.differences)
                + "\nThe constants would belong to the generated core; allow it with --allow-differences "
                  "(allow_differences=True).")
        for d in report.differences:
            warnings.append(f"the generated core differs from the .xci: {d}")
        cfg = report.config
    names = case_names(cfg, cases, extra)
    if cfg.points < SMALLEST_N:
        warnings.append(f"{cfg.points} points: the bench's cases are written for {SMALLEST_N} points and up; the "
                        f"extraction is expected to report problems")
    env = dict(os.environ, XFFT_CONFIG=cfg.spec, SIM_CACHE=cache)
    if cache_url:
        env["SIM_CACHE_URL"] = str(cache_url)
    if elab_cache is not None:
        env["SIM_ELAB_CACHE"] = elab_cache
    if elab_cache_url:
        env["SIM_ELAB_CACHE_URL"] = str(elab_cache_url)
    for w in warnings:
        say(f"characterize: warning: {w}")
    say(f"characterize: {cfg.tag} (XFFT_CONFIG={cfg.spec}), {len(names)} cases, models {', '.join(models)}, "
        f"cache {cache} {env.get('SIM_CACHE_URL') or sim_cache.DEFAULT_URL}, elaboration cache "
        f"{env.get('SIM_ELAB_CACHE', 'on')} "
        f"{env.get('SIM_ELAB_CACHE_URL') or env.get('SIM_CACHE_URL') or sim_cache.DEFAULT_URL}")

    problems: list[str] = []
    if "rtl" in models:
        say("characterize: the cases on the real core (rtl)")
        failed = [r for r in run.run_phase(cfg.tag, names, "rtl", jobs, env, say) if not r.passed]
        problems += [f"{r.case} failed on the real core, see {r.log}" for r in failed]

    say("characterize: extraction")
    bench_json = bench_cases.OUT / f"{cfg.tag}.json"
    before = bench_json.stat().st_mtime_ns if bench_json.is_file() else None
    rc, lines = run.run_script("extract.py", [], env, say)
    written = bench_json.is_file() and bench_json.stat().st_mtime_ns != before
    doc = json.loads(bench_json.read_text()) if written else None
    problems += (doc or {}).get("problems", [])
    problems += [line.removeprefix("extract: ") for line in lines
                 if line.startswith("extract: no ") or (doc is None and line.startswith("extract: PROBLEM"))]
    if rc != 0 and not problems:
        problems.append(f"the extraction failed (exit code {rc})")

    compared, comparison = None, []
    if "engine" in models and doc is not None and not problems:
        say("characterize: the cases on the stand-in (engine), built from the extracted constants")
        failed = [r for r in run.run_phase(cfg.tag, names, "engine", engine_jobs, env, say) if not r.passed]
        comparison += [f"{r.case} failed on the stand-in, see {r.log}" for r in failed]
        say("characterize: comparison")
        rc, lines = run.run_script("compare.py", names, env, say)
        comparison += [line.strip() for line in lines if line.strip().startswith("FAIL")]
        if rc != 0 and not comparison:
            comparison.append(f"the comparison failed (exit code {rc})")
        compared = not comparison
    elif "engine" in models:
        say("characterize: the stand-in is not run: no clean constants to build it from")

    json_path = bench_json if doc is not None else None
    if json_path is not None and out is not None:
        target = out_path(out, cfg.tag)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() != json_path.resolve():
            shutil.copyfile(json_path, target)
        json_path = target
    code = EXIT_PROBLEMS if problems or doc is None else EXIT_COMPARE if compared is False else EXIT_OK
    result = Result(config=cfg, constants=(doc or {}).get("constants"), problems=problems, json_path=json_path,
                    compared=compared, comparison=comparison, warnings=warnings, exit_code=code)
    say(f"characterize: {'PASS' if result.ok else 'FAIL'} ({code}): "
        + (f"{len(problems)} problem(s)" if problems else "constants extracted without problems")
        + ("" if compared is None else ", the stand-in matches the core" if compared
           else f", the stand-in differs from the core ({len(comparison)})")
        + (f"; JSON: {json_path}" if json_path else "; no JSON written"))
    return result


def summary(result: Result) -> dict:
    """A JSON-friendly summary of a Result (what the CLI prints with --summary-json)."""
    out = dataclasses.asdict(result)
    out["config"] = dataclasses.asdict(result.config)
    out["json_path"] = str(result.json_path) if result.json_path else None
    out["tag"] = result.tag
    return out
