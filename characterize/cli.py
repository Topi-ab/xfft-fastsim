"""Command line of the characterization: python -m characterize --help.

A thin wrapper over characterize.api.characterize(): the arguments become its parameters, its progress lines go
to stdout, its Result becomes the exit code (0: constants extracted without problems, and the stand-in matched
the core when compared; 1: problems; 2: bad arguments, a Versal part, an unsupported .xci or no Vivado; 3: clean
constants, but the stand-in differs from the core).
"""

from __future__ import annotations

import argparse
import json
import sys

import xfft_config

from . import api
from . import xci as xci_reader

EPILOG = """\
examples:
  python -m characterize --nfft 8                         the 256-point core, other fields default
  python -m characterize --points 4096 --input-width 18 --twiddle-width 18 --rounding truncation \\
      --part xczu4eg-sfvc784-1-e --clock-mhz 300 -j 8 --out timing/
  python -m characterize --xci path/to/xfft_0.xci -j 8    the configuration of an existing IP
  python -m characterize --nfft 8 --dry-run               show the configuration and the cases, run nothing
  python -m characterize --nfft 8 --models rtl            real core and extraction only, no comparison

exit codes:
  0  constants extracted without problems (and the stand-in matched the core, when compared)
  1  the extraction has problems or real-core cases failed: no clean constants
  2  bad arguments or configuration, a Versal part (not supported), an unsupported .xci, Vivado not on PATH
  3  constants extracted without problems, but the stand-in differs from the core

The base configuration is XFFT_CONFIG (or --config); the field options override it. Results and elaborated
snapshots are cached (docs/CACHE.md): a second run with the same inputs takes seconds. See docs/CHARACTERIZATION.md.
"""

FIELDS = (("nfft", int, "log2 of the transform length (3 .. 16)"),
          ("points", int, "the transform length, a power of two (instead of --nfft)"),
          ("input_width", int, "data bits per component (8 .. 34)"),
          ("twiddle_width", int, "phase factor bits (8 .. 34)"),
          ("rounding", str, "convergent or truncation"),
          ("part", str, "the FPGA part, e.g. xc7a35tcpg236-1"),
          ("clock_mhz", int, "target clock frequency of the IP in MHz"),
          ("module", str, "module name of the core (default xfft_core)"))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m characterize", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Measure the timing constants of an xfft core configuration for the stand-in: generate the "
                    "core with Vivado, run the characterization cases on it in XSim, extract the constants into a "
                    "JSON file, and check the stand-in built from them against the core.")
    g = p.add_argument_group("configuration")
    g.add_argument("--config", metavar="SPEC", help="an XFFT_CONFIG value, e.g. nfft=8,rounding=truncation "
                                                    "(default: the XFFT_CONFIG environment variable)")
    for name, kind, text in FIELDS:
        g.add_argument("--" + name.replace("_", "-"), dest=name, type=kind, metavar=name.upper(), help=text)
    g.add_argument("--xci", metavar="PATH", help="read the configuration from an existing xfft .xci with Vivado "
                                                 "(instead of the options above)")
    g.add_argument("--allow-differences", action="store_true",
                   help="with --xci: characterize even when the core the bench generates differs from the IP in "
                        "options that are not parameters of the bench (memory types, IP version, ...)")
    r = p.add_argument_group("run")
    r.add_argument("cases", nargs="*", help="only these cases (default: all; the extraction needs every pinning "
                                            "case)")
    r.add_argument("--models", default="rtl,engine",
                   help="rtl (real core and extraction), engine (stand-in and comparison) or rtl,engine (default); "
                        "cmodel is accepted for engine")
    r.add_argument("-j", "--jobs", type=int, default=2, help="real-core simulations at a time (default 2)")
    r.add_argument("--engine-jobs", "--cmodel-jobs", dest="engine_jobs", type=int, default=6,
                   help="stand-in simulations at a time (default 6)")
    r.add_argument("--extra", action="store_true", help="also the extra comparison cases (not used by the "
                                                        "extraction)")
    r.add_argument("--cache", choices=("on", "off", "refresh"), default=None,
                   help="the result cache (default: SIM_CACHE, else on)")
    r.add_argument("--cache-url", metavar="URL", help="the cache's store: a directory or s3://bucket/prefix "
                                                      "(default: SIM_CACHE_URL, else .sim_cache/)")
    r.add_argument("--elab-cache", choices=("on", "off", "refresh"), default=None,
                   help="the elaboration cache: snapshots reused across runs and machines (default: SIM_ELAB_CACHE, "
                        "else on)")
    r.add_argument("--elab-cache-url", metavar="URL", help="the elaboration cache's store (default: "
                                                           "SIM_ELAB_CACHE_URL, else the result cache's)")
    r.add_argument("--out", metavar="PATH", help="also write the JSON here: a .json file or a directory "
                                                 "(<dir>/<tag>.json); timing/ makes the stand-in use it")
    r.add_argument("--summary-json", metavar="PATH", help="write a summary of the result (constants, problems, "
                                                          "exit code, JSON path) as JSON")
    r.add_argument("--dry-run", action="store_true", help="print the configuration and the cases, run nothing")
    r.add_argument("--list-cases", action="store_true", help="list the cases with their purpose, run nothing")
    return p


def config_of(args, p: argparse.ArgumentParser) -> xfft_config.XfftConfig:
    """The configuration from --config / XFFT_CONFIG and the field options."""
    import os

    try:
        base = xfft_config.parse(args.config if args.config is not None else os.environ.get("XFFT_CONFIG", ""))
        fields = {name: getattr(args, name) for name, _, _ in FIELDS if getattr(args, name) is not None}
        points = fields.pop("points", None)
        if points is not None:
            if "nfft" in fields:
                p.error("--points and --nfft: give one")
            if points < 1 or points & (points - 1):
                p.error(f"--points {points}: not a power of two")
            fields["nfft"] = points.bit_length() - 1
        return xfft_config.XfftConfig(**{**{f: getattr(base, f) for f in base.__dataclass_fields__}, **fields})
    except ValueError as e:
        p.error(f"configuration: {e}")


def say(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    import os

    field_given = any(getattr(args, name) is not None for name, _, _ in FIELDS) or args.config is not None
    if args.xci and field_given:
        p.error("--xci reads the whole configuration: no --config or field options with it")
    if args.allow_differences and not args.xci:
        p.error("--allow-differences goes with --xci")
    models = list(dict.fromkeys(api.MODEL_ALIASES.get(m, m) for m in args.models.split(",") if m))
    cache = args.cache or os.environ.get("SIM_CACHE", "on")
    try:
        if args.xci:
            if args.dry_run or args.list_cases:
                report = xci_reader.read_config(args.xci, log=say)
                for u in report.unsupported:
                    print(f"characterize: unsupported: {u}")
                for d in report.differences:
                    print(f"characterize: differs: {d}")
                if report.unsupported:
                    return api.EXIT_USAGE
                if report.differences and not args.allow_differences:
                    print("characterize: a run would refuse these differences without --allow-differences")
                    return api.EXIT_USAGE
                cfg = report.config
            else:
                cfg = None
        else:
            cfg = config_of(args, p)
            api.check_part(cfg)
        if args.dry_run or args.list_cases:
            names = api.case_names(cfg, args.cases, args.extra)
            print(f"characterize: {cfg.tag} (XFFT_CONFIG={cfg.spec}), {len(names)} cases, models "
                  f"{', '.join(models)}")
            if args.list_cases:
                every = api.bench_cases.cases(cfg.points, extra=True)
                for c in names:
                    kind = " (extra)" if every[c].extra else " (known approximation)" if every[c].known else ""
                    print(f"  {c:30s}{kind} {every[c].purpose}")
            else:
                print("  " + " ".join(names))
            return api.EXIT_OK
        result = api.characterize(cfg, xci=args.xci, allow_differences=args.allow_differences, models=models,
                                  jobs=args.jobs, engine_jobs=args.engine_jobs, cases=args.cases or None,
                                  extra=args.extra, cache=cache, cache_url=args.cache_url,
                                  elab_cache=args.elab_cache, elab_cache_url=args.elab_cache_url, out=args.out,
                                  log=say)
    except (api.CharacterizeError, xci_reader.XciError) as e:
        print(f"characterize: error: {e}", file=sys.stderr)
        return api.EXIT_USAGE
    if args.summary_json:
        with open(args.summary_json, "w") as f:
            json.dump(api.summary(result), f, indent=1, sort_keys=True)
            f.write("\n")
    return result.exit_code
