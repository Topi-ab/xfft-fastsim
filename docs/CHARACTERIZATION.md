# Characterization: the timing constants of your core

The characterization runs in XSim only: it needs the real core, whose simulation model is encrypted IP, so it never runs on NVC or GHDL. The constants it writes serve the stand-in (the C engine) on every simulator.

The stand-in's timing comes from 16 constants per configuration. The repository ships them for three configurations ([README](../README.md#configuration)). For any other configuration, or after a Vivado update, measure them with the characterization: it takes an FFT configuration (or an existing `.xci`), generates the core with Vivado, simulates a fixed set of short scenarios on the real core in XSim, derives every constant from them with explicit formulas and cross-checks, and writes the result as JSON. Then it builds the stand-in from the new constants, runs the same scenarios on it and compares the two models clock for clock.

Supported device families: 7-series, Zynq UltraScale+ and Virtex UltraScale+. Versal parts are refused (exit code 2, `CharacterizeError` in the API) before any core is generated: on Versal the real core's XSim simulation outputs all-zero data for any input (its handshake timing is right), so it gives no reference to characterize against. The part name decides (Versal names start with `xcvc`, `xcve`, `xcvm`, `xcvp`, `xcvh`, `xcvr`, their `xqv`/`xav` variants, or `xc2v` for Versal Gen 2; `xcvu` is Virtex UltraScale+ and is supported); with `--xci`, Vivado's own `ARCHITECTURE` and `FAMILY` of the part decide.

```
characterize: error: part xcvc1902-vsva2197-2MP-e-S: Versal is not supported by the characterization: on Versal the real xfft core's XSim simulation outputs all-zero data for any input, so it gives no reference to characterize against (supported: 7-series, Zynq UltraScale+, Virtex UltraScale+)
```

It runs from the repository root as a command (`python -m characterize`) or as a Python function (`characterize.characterize()`); the command is a thin wrapper over the function, and both give the same JSON.

## Running it

Vivado's `settings64.sh` sourced and the venv active (see the [README](../README.md#quick-start)):

```sh
python -m characterize --nfft 8                                  # the 256-point core, other fields default
python -m characterize --points 4096 --input-width 18 --twiddle-width 18 --rounding truncation \
    --part xczu4eg-sfvc784-1-e --clock-mhz 300 -j 8 --out timing/
python -m characterize --xci path/to/xfft_0.xci -j 8             # the configuration of an existing IP
python -m characterize --nfft 8 --dry-run                        # the configuration and the cases, nothing run
python -m characterize --help
```

Options:

| Option | Meaning |
|---|---|
| `--nfft`, `--points`, `--input-width`, `--twiddle-width`, `--rounding`, `--part`, `--clock-mhz`, `--module` | the configuration's fields; they override `--config` |
| `--config SPEC` | an `XFFT_CONFIG` value as the base (default: the `XFFT_CONFIG` environment variable, else the default configuration) |
| `--xci PATH` | read the configuration from an existing xfft `.xci` with Vivado (below), instead of the options above |
| `--allow-differences` | with `--xci`: go on when the core the bench generates differs from the IP in an option that is not a bench parameter |
| `--models` | `rtl,engine` (default): real core, extraction, stand-in, comparison; `rtl`: real core and extraction only (`cmodel` is accepted for `engine`) |
| `-j N`, `--jobs N` | real-core simulations at a time (default 2), one CPU core and a few hundred MB each |
| `--engine-jobs N` | stand-in simulations at a time (default 6; `--cmodel-jobs` is accepted for it) |
| `cases ...` | only these cases (the extraction needs all pinning cases, so this is for looking at single cases) |
| `--extra` | also the extra comparison cases (below) |
| `--cache on\|off\|refresh`, `--cache-url URL` | the result cache ([CACHE.md](CACHE.md)); default `SIM_CACHE` / `SIM_CACHE_URL`, else on and `.sim_cache/` |
| `--elab-cache on\|off\|refresh`, `--elab-cache-url URL` | the elaboration cache ([CACHE.md](CACHE.md#elaboration-cache)); default `SIM_ELAB_CACHE` / `SIM_ELAB_CACHE_URL`, else on and the result cache's store |
| `--out PATH` | also write the JSON there: a `.json` file, or a directory (`<dir>/<tag>.json`); `--out timing/` makes the stand-in use it at once |
| `--summary-json PATH` | a summary of the result (constants, problems, comparison, exit code, JSON path) as JSON |
| `--list-cases` | the cases with their purpose |

Progress goes to stdout: one line per finished case (`pass`/`FAIL`, cache `hit`/`miss`, seconds), the extraction's report, the comparison's report, and a last line with the verdict and the JSON's path. Each case's full log is in `build/characterize/<tag>/logs/<case>-<model>.log`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | constants extracted without problems (and, with the stand-in run, the stand-in matched the real core) |
| 1 | the extraction has problems, or real-core cases failed: no clean constants |
| 2 | bad arguments or configuration, a Versal part, an unsupported `.xci`, Vivado not on `PATH` |
| 3 | constants extracted without problems, but the stand-in differs from the real core on some case |

With code 1 the JSON is still written when every constant has evidence; its `problems` field lists what did not fit. With code 3 the constants are fine to use; the comparison's `FAIL` lines say where the stand-in differs (compare with "Known limitations" in the [user guide](USER_GUIDE.md#known-limitations)).

### Python API

```python
import sys
sys.path.insert(0, "/path/to/the/repository")         # not needed when running from the repository root
from characterize import characterize, CharacterizeError

result = characterize("nfft=8", jobs=4)                # an XFFT_CONFIG string, a dict of fields, an XfftConfig
print(result.ok, result.exit_code)                     # True, 0 when the constants came out clean
print(result.constants["LATENCY_FIRST_OUT"])
print(result.problems)                                 # [] or what the extraction could not explain
print(result.json_path)                                # the JSON (a copy at out=..., else the bench's own)
doc = result.document                                  # the JSON as a dict

result = characterize(xci="ip/xfft_0/xfft_0.xci", jobs=8, out="timing/")
```

`characterize(config=None, *, xci=None, allow_differences=False, models=("rtl", "engine"), jobs=2, engine_jobs=6, cases=None, extra=False, cache="on", cache_url=None, elab_cache=None, elab_cache_url=None, out=None, log=print)` (`"cmodel"` in `models` and `cmodel_jobs=` are accepted for `"engine"` and `engine_jobs=`). It raises `CharacterizeError` when it cannot start (a bad configuration, a Versal part, an unsupported `.xci`, no Vivado); everything after that is in the returned `Result`: `config`, `tag`, `constants`, `problems`, `json_path`, `compared` (True, False, or None when the stand-in was not run), `comparison` (the comparison's failures), `warnings`, `exit_code`, `ok`. `log=None` makes it quiet; any callable gets the progress lines. `characterize.read_config(xci)` and `characterize.config_from_xci(xci)` read an `.xci` without characterizing it.

## Starting from an existing `.xci`

`--xci PATH` (`xci=` in the API) opens the IP with Vivado in batch mode (`scripts/read_xci.tcl`: `read_ip` in an in-memory project, on a copy, so nothing is written next to your file) and reads its IP definition, its part (with the part's `ARCHITECTURE` and `FAMILY`) and every `CONFIG.*` property as Vivado resolves them. An IP for a Versal part is refused right there, before the bench's core is generated. They map to the bench's configuration:

| `.xci` | Configuration |
|---|---|
| `CONFIG.transform_length` | `nfft` (a power of two) |
| `CONFIG.input_width`, `CONFIG.phase_factor_width` | `input_width`, `twiddle_width` |
| `CONFIG.rounding_modes` | `rounding` (`convergent_rounding`, `truncation`) |
| `CONFIG.target_clock_frequency` | `clock_mhz` (whole MHz) |
| `CONFIG.Component_Name` | `module` |
| `PART` | `part` |

The options the stand-in is written for must have its values: `implementation_options` = `pipelined_streaming_io`, `data_format` = `fixed_point`, `scaling_options` = `scaled`, `output_ordering` = `natural_order`, `throttle_scheme` = `nonrealtime`, `xk_index`, `ovflo` and `aresetn` = `true`, `aclken`, `run_time_configurable_transform_length` and `cyclic_prefix_insertion` = `false`, `channels` and `super_sample_rates` = 1, and an xfft 9.x IP definition. Every option that differs is reported, and the run stops with exit code 2:

```
characterize: error: path/to/xfft_0.xci: options the stand-in does not support:
  CONFIG.scaling_options = block_floating_point: scaling: fixed scaling (scaled) only, SCALE_SCH on the config channel (scaled)
  CONFIG.throttle_scheme = realtime: throttling: non-real-time only (the core honours m_axis_data_tready) (nonrealtime)
```

The bench always generates its own core from the mapped configuration (`ip/xfft_core.tcl`, the newest xfft 9.x of your Vivado), so options that are not bench parameters (memory types, butterfly and multiplier types, the stages in block RAM, the target throughput) get Vivado's defaults there. The `.xci` reader therefore also reads the generated core's `.xci` with Vivado and compares every option and the IP version. A difference can change the latency, so by default the run stops (exit code 2) and lists them; `--allow-differences` goes on, with the differences printed as warnings, and the constants then belong to the generated core. `--xci ... --dry-run` shows the mapping and the differences without simulating; it still starts Vivado twice (reading the `.xci`, and generating and reading the bench's core), so it takes a minute or so the first time.

## What it costs

Each case is one XSim run of 1 to 9 frames, mostly 1 to 3; the default set has 135 cases (134 pinning cases and one known approximation). The real core simulates at a few hundred clocks per second, so the time grows about linearly with N. Rough figures on a current x86-64 machine, per case on the real core, without the one-off IP generation (a minute or two) and elaboration:

| N | per case (real core) | 135 cases, `-j 2` | 135 cases, `-j 8` |
|---|---|---|---|
| 256 | 5 to 8 s | 6 to 9 min | 2 to 3 min |
| 512 | about 10 s | 12 to 15 min | 3 to 5 min |
| 1024 | about 15 s (up to 40 s) | 20 to 25 min | 5 to 8 min |
| 2048 and more | doubling with each doubling of N | | |

The stand-in phase adds about 5 s per case, divided by `--engine-jobs` (about 2 minutes with 6). More jobs help up to the number of CPU cores. A rerun with the same inputs is all cache hits and takes seconds per case, also on another machine that shares the cache store ([CACHE.md](CACHE.md)); each case is still its own pytest process, so a fully cached run at 256 points takes a minute or so per phase. Each model's snapshot is built once per configuration and goes into the elaboration cache ([CACHE.md](CACHE.md#elaboration-cache)): a run with new cases or new constants on another machine with the same Vivado installation path restores the real core's snapshot instead of generating and elaborating it. The real core's snapshot of the flow tests (`tests/test_xfft_flow.py`) of the same configuration has the same key, so either one serves the other.

The smallest transform length the bench can characterize is 256 points: below that some cases give no evidence for some constants, and the extraction reports problems (exit code 1).

## The cases

`python -m characterize --list-cases` lists them with their purpose (`characterize/bench/cases.py`). Each is a short scenario of input gaps (`s_axis_data_tvalid` low) and output stalls (`m_axis_data_tready` low), on all-zero data unless it needs an overflow:

- `ideal`, `back_to_back`: latencies, reset to ready, the status word, the config channel;
- `gap_*`, `gaps_*`, `idle_*`: the input slack: which gaps are free and what the others cost, at a frame start and after idle clocks;
- `stall_*`, `stalls_1_5`: the stall cost and the queue behind the output (`OUTPUT_HALT_EVENT_AFTER`, `OUTPUT_BUFFER`);
- `starve`, `halt_*`: what input gaps do to the output of earlier frames;
- `blocked`, `fifo_backlog`, `release_*`, `dribble_*`: a blocked output, the input buffer (`INPUT_LIMIT`) and the input's resume (`INPUT_RESUME`);
- `stall_end_<k>_100`, `stall_end_stream_<k>_100`: a long stall before each of the last 40 samples, at the end of the last frame and in a stream (`PIPELINE_LOOKAHEAD`, `OUTPUT_QUEUE_SIZE`);
- `stall_then_halt`, `short_stall_then_halt`, `stall_end_idle*`, `dense_stalls_1`: combinations that cross-check the rules;
- `dense_backpressure`: a known approximation (see the user guide), compared with a tolerance of a few clocks in the input handshakes; at some configurations it matches exactly, which the comparison notes;
- with `--extra`: 42 extra comparison cases that combine the phenomena (gaps and stalls together, several stalls per frame, blocked outputs released at odd points, starvation while earlier frames stream out). The extraction does not use them. Several of them combine input gaps with a paused pipeline, where the stand-in is known to differ, so a run with `--extra` can end with exit code 3.

A run records facts only: every handshake clock of the four channels, the clocks the event outputs are high, the clocks of `tready` after reset, and a checksum of each frame's output. The extraction derives each constant from the cases that carry evidence for it, requires all of them to agree, and checks that the rules of `model/xfft_timing.py` explain every other case with the derived constants. Anything that does not fit is a problem.

## The JSON

`build/characterize/out/<tag>.json`, and a copy at `--out`. It holds no times or paths, so the same inputs give the same file, byte for byte, from the command line and from the API (with the same cache store, since `provenance` lists cache keys).

| Field | What |
|---|---|
| `tag` | the configuration's tag, e.g. `n256_d16_t16_conv_xc7a35tcpg236-1_100mhz` |
| `config` | the configuration's fields (`XfftConfig`): `nfft`, `input_width`, `twiddle_width`, `scaling`, `rounding`, `part`, `clock_mhz`, `module` |
| `core_version` | the xfft version of the simulated core, e.g. `9.1` |
| `constants` | the 16 timing constants, `NAME: value` |
| `evidence` | per constant (and per derived quantity) the list of `[case and what was measured, value]` it was derived from |
| `relations` | consistency checks between the constants (`RELATIONS` in `model/xfft_timing.py`): `name: [left side, right side]`; reported, never assumed |
| `provenance` | `extractor_version`, and `rtl_runs`: the result-cache key of each real-core case |
| `problems` | only when there are any: what the extraction could not explain |

The constants (all clock counts between AXI4-Stream handshakes, from a frame's first input handshake unless stated otherwise):

| Constant | Meaning |
|---|---|
| `LATENCY_FIRST_OUT`, `LATENCY_LAST_OUT` | to the first and the last (`tlast`) output handshake, no gaps or stalls |
| `INPUT_SLACK` | the input slack: a gap that fits into it is free |
| `OUTPUT_STALL_COST` | clocks of delay per clock of `m_axis_data_tready` low |
| `RESET_TO_READY` | first clock with `aresetn` high to both input `tready` high |
| `CONFIG_ACCEPT_LOADING` | wait of a config word while a frame loads |
| `FRAME_BOUNDARY_STALL` | input stall between frames sent back to back |
| `STARVE_HALTS_OUTPUT` | 1: an input gap's cost also holds up the output of earlier frames |
| `OUTPUT_HALT_EVENT_AFTER` | samples waiting behind a stalled output at which the pipeline pauses |
| `FRAME_STARTED_DELAY` | first input handshake to `event_frame_started` |
| `STATUS_AFTER_LAST_OUT` | last output handshake to the status word |
| `INPUT_LIMIT` | samples the core takes with its output blocked from the start |
| `OUTPUT_BUFFER` | samples waiting at which a paused pipeline resumes |
| `INPUT_RESUME` | samples that must leave a full input buffer before input resumes |
| `PIPELINE_LOOKAHEAD` | samples the pipeline and the status logic see ahead |
| `OUTPUT_QUEUE_SIZE` | samples the queue behind the output register holds |

The rules that tie them to the handshakes, clock by clock, are in the docstring of `model/xfft_timing.py`.

## Using the result

- **`timing/<tag>.json`.** Put the JSON there (`--out timing/`, or copy it). `model/xfft_timing.constants()` reads its `constants` for that tag, so `scripts/build_engine.py`, the cocotb tests and the timing bench use it at once. Commit it with your repository.
- **`TABLE`.** Or paste the constants into `TABLE` in `model/xfft_timing.py`; the extraction prints the entry ready to paste. `TABLE` wins over `timing/`.

Then check the stand-in in your own traffic: `pytest tests/test_xfft_compare.py` (real core and stand-in side by side) and your tests on both models. A comparison failure that is not one of the known limitations is worth an issue (`.github/ISSUE_TEMPLATE/`).

## Files

| Path | What |
|---|---|
| `characterize/cli.py`, `characterize/api.py`, `characterize/xci.py` | the command line, the API, the `.xci` reader |
| `characterize/bench/cases.py` | the cases |
| `characterize/bench/test_characterize.py` | the cocotb test that runs one case and records its facts |
| `characterize/bench/extract.py` | derives the constants, writes the JSON (`--from engine`: the same on the stand-in's facts, a self-check) |
| `characterize/bench/compare.py` | compares the two models case by case |
| `characterize/bench/run.py`, `snapshots.py`, `build_engine_char.py` | runs the cases in parallel; builds (or restores from the elaboration cache) each model once per configuration; builds the stand-in from the extracted constants |
| `build/characterize/<tag>/` | snapshots (the stand-in's with its library in its `dpi/`), one run directory per case and model with a copy of `xsim.dir/` and `dpi/`, `logs/` |
| `build/characterize/out/<tag>.json` | the result |
