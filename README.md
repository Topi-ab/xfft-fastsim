# xfft-fastsim

A fast simulation stand-in for AMD's LogiCORE FFT (xfft 9.x) in the Vivado simulator (XSim), and in Riviera-PRO, NVC, GHDL and Verilator. It has the generated core's name and ports: the numbers come from AMD's bit-accurate C model of the core, and the AXI4-Stream handshakes follow the core's cycle timing, described by a small set of timing constants per configuration. Compile it in place of the core and your testbench runs unchanged, much faster than with the core's encrypted simulation model.

xfft-fastsim is an independent project, not affiliated with or endorsed by AMD.

The stand-in is **the C engine** (`model/xfft_engine.c`, `FFT_MODEL=engine`): all of its timing is in C, behind thin HDL wrappers that make no timing decisions of their own, a SystemVerilog module for **XSim, Riviera-PRO and Verilator** (`model/xfft_engine.sv`, DPI-C) and a VHDL-2008 entity for **NVC and GHDL** (`model/xfft_engine.vhdl`, VHPIDIRECT); the same C files on all five simulators (docs/USER_GUIDE.md).

Only generating the core, which brings the C model, needs Vivado, once per configuration. The real core runs in XSim, and in Riviera-PRO with AMD's simulation libraries compiled for it (`XFFT_RIVIERA_LIBS`, docs/USER_GUIDE.md): the tests against the real core (`tests/test_xfft_compare.py`) run on both; the characterization and the timing bench in `tests/timing/` are XSim only. NVC, GHDL and Verilator run the stand-in only.

**Supported architecture: Pipelined, Streaming I/O only.** The stand-in, its timing constants and the characterization cover the core's pipelined streaming architecture. The burst architectures (Radix-4, Radix-2 and Radix-2 Lite Burst I/O) are not supported yet; the tools refuse them (see Limitations).

**Supported device families: 7-series, Zynq UltraScale+, Virtex UltraScale+. Versal is not supported.** On Versal the real core's XSim simulation outputs all-zero data for any input (its handshake timing is right), so there is no real-core reference to check against, and the characterization refuses a Versal part before it generates anything.

What is in the repository:

- the stand-in (`model/`) and the scripts that build it with the C model from your Vivado installation (`scripts/`);
- a cocotb test infrastructure that runs the same tests on the real core or on the stand-in (`FFT_MODEL=rtl|engine`, `tests/`), on XSim and, for the stand-in, on NVC, GHDL and Verilator where they are installed, with a result cache and an elaboration cache that can be shared through S3;
- a characterization tool that measures the timing constants of your own core configuration on the real core and writes them as JSON (`python -m characterize`, also with a Python API, also from an existing `.xci`);
- examples (`examples/`) and documentation (`docs/`).

Tested with Vivado 2025.2 (xfft 9.1) on x86-64 Linux. Other Vivado versions are not refused and are expected to work; the tools print a warning when they generate a core with another version. Measure the timing constants of your configuration with your Vivado (docs/CHARACTERIZATION.md), since another core version can have another latency.

## Requirements

- Linux x86-64.
- Vivado with XSim, the FFT IP (xfft) and the device family of your part installed. Vivado provides `vivado`, `xvlog`, `xvhdl`, `xelab` and `xsim`.
- `gcc` (or another C compiler named by `CC`) for the C engine's library.
- Python 3.12 or newer with `venv` (the pinned numpy needs it), and the shared Python library that cocotb loads into the simulator (`libpython3.X`; on Debian and Ubuntu it comes with `python3-dev`, or as `libpython3.12t64` on Ubuntu 24.04).
- The Python packages in `requirements.txt` (cocotb, cocotb-vivado, pytest, numpy; boto3 only for an S3 cache, moto only for the cache's unit tests).
- For the stand-in on NVC, GHDL or Verilator: NVC (tested with 1.23), GHDL with the LLVM or GCC backend (tested with 6.0) or Verilator (tested with 5.036, single-threaded; with a C++ compiler and `make`); plus the generated core's `build/<tag>/ip_gen/` from a machine with Vivado.
- For Riviera-PRO (tested with 2026.04): its `vsimsa` on PATH; for the real core also AMD's simulation libraries compiled for Riviera with Vivado (`compile_simlib -simulator riviera`), their directory in `XFFT_RIVIERA_LIBS`.
- For `tests/test_timing_reference.py` only: a C++ compiler (`c++`, `g++`, or `CXX`) for the reference data's unpacker.

## Quick start

```sh
source /path/to/Vivado/<version>/settings64.sh       # puts the Vivado tools on PATH
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate

examples/plain_xsim/run.sh                           # one FFT through the stand-in in plain XSim
FFT_MODEL=engine pytest examples/                    # the cocotb examples on the stand-in
FFT_MODEL=engine pytest tests/test_xfft_flow.py      # flow-control tests on the stand-in: XSim, Riviera, NVC, GHDL, Verilator
FFT_MODEL=rtl pytest tests/test_xfft_flow.py         # the same tests on the real core
pytest tests/test_xfft_compare.py                    # real core and stand-in side by side, word by word
pytest tests/test_xfft_trace.py                      # the stand-in on every simulator, clock by clock
pytest tests/unit                                    # no Vivado needed
pytest tests/test_timing_reference.py                # the stand-in against stored real-core timing: no Vivado, NVC or GHDL
```

The first run of a configuration generates the core with Vivado (a minute or two); later runs reuse it. Elaborated snapshots are cached by their build inputs, also across checkouts and machines with the same Vivado installation path, so a second run skips `xvlog`, `xvhdl` and `xelab` (docs/CACHE.md, "Elaboration cache", which also covers what makes a snapshot portable; `SIM_ELAB_CACHE=off` switches it off). pytest prints the Python environment, the model and the configuration in its header. A bare `pytest` runs `tests/` and `examples/` (`pytest.ini`), never the characterization bench.

To use the stand-in in your own XSim testbench, three steps do it (docs/USER_GUIDE.md has the details, `examples/plain_xsim/run.sh` does exactly this):

```sh
XFFT_CONFIG=nfft=8 python scripts/build_engine.py build/engine  # the C model, the engine's library, the packages
xvlog -sv -work xil_defaultlib build/engine/xfft_model_pkg.sv model/xfft_engine.sv <your sources>
xelab -L xil_defaultlib -sv_root build/engine -sv_lib libxfft_engine -snapshot top xil_defaultlib.<your top>
```

## Where the AMD parts come from

Nothing of AMD's is in this repository. Everything is generated from your Vivado installation, found through `PATH` (the `settings64.sh` of your Vivado):

- `scripts/generate_ip.py` writes a builder Tcl for the configuration (`ip/xfft_core.tcl` plus the parameters) and runs it with `vivado -mode batch` in `build/<tag>/ip_gen/`. It creates the core in an in-memory project with the newest xfft 9.x the installation has, generates its simulation files and its C model (the IP's `cmodel` output product): `build/<tag>/ip_gen/ip/<module>/cmodel/*_bitacc_cmodel_lin64.zip`. So the C model always matches the core.
- `scripts/build_engine.py <dir>` runs that when needed, unzips the C model into `<dir>/cmodel/`, reads its header, library and API names from the archive, compiles the C engine (`model/xfft_engine.c` with `model/xfft_dpi.c`) against it with `gcc` and writes the SV and VHDL packages `xfft_model_pkg`. The library is linked with the rpath `$ORIGIN/cmodel`, the `cmodel/` beside it (the C model's own gmp, mpfr and floating-point libraries are in the same archive), so no `LD_LIBRARY_PATH` is needed and the directory can be moved.
- For the real core, the cocotb runs generate the IP from the same builder Tcl through cocotb-vivado and take the XSim libraries from the simulation scripts Vivado exports for it.

No Vivado path or version is written in the code. `build/` holds everything generated and is safe to delete.

## Configuration

A configuration is `model/xfft_config.XfftConfig`. The environment variable `XFFT_CONFIG` overrides fields of the default, as a comma-separated list, for every tool and test of the repository: `XFFT_CONFIG=nfft=12,input_width=18,rounding=truncation`.

| Field | Default | Values | Meaning |
|---|---|---|---|
| `nfft` | 10 | 3 .. 16 | log2 of the transform length N |
| `input_width` | 16 | 8 .. 34 | data bits per component, input and output (scaled) |
| `twiddle_width` | 16 | 8 .. 34 | phase factor bits |
| `rounding` | `convergent` | `convergent`, `truncation` | rounding mode of the core |
| `part` | `xc7a35tcpg236-1` | any installed part | the FPGA part the core is generated for |
| `clock_mhz` | 100 | whole MHz | target clock frequency of the IP |
| `module` | `xfft_core` | a module name | the generated core's name, and the stand-in's; its package, library and C functions are named after it, so that configurations with different modules can share one simulation ([docs/USER_GUIDE.md](docs/USER_GUIDE.md#several-configurations-in-one-simulation)) |
| `scaling` | `scaled` | `scaled` | fixed scaling with a per-frame schedule (the only one supported) |

Fixed by `ip/xfft_core.tcl`: pipelined streaming I/O, fixed point, natural-order output, non-real-time throttling (the core honours `m_axis_data_tready`), `XK_INDEX` and `OVFLO` in `m_axis_data_tuser`, `aresetn`, no `aclken`, one channel. Forward or inverse and the scaling schedule come per frame on the config channel.

Each configuration has a tag, for example `n256_d16_t16_conv_xc7a35tcpg236-1_100mhz`, which names its build directory and its timing constants. The stand-in needs the constants of exactly its configuration. These ship with the repository:

| Configuration | Tag | Where |
|---|---|---|
| default (1024 points, 16/16 bits, convergent, xc7a35tcpg236-1, 100 MHz) | `n1024_d16_t16_conv_xc7a35tcpg236-1_100mhz` | `TABLE` in `model/xfft_timing.py` |
| `nfft=8` (256 points, otherwise default) | `n256_d16_t16_conv_xc7a35tcpg236-1_100mhz` | `timing/` |
| `nfft=9,input_width=18,twiddle_width=18,rounding=truncation,clock_mhz=250` | `n512_d18_t18_trun_xc7a35tcpg236-1_250mhz` | `timing/` |

For any other configuration, measure them: `python -m characterize --nfft 12 --input-width 18 --out timing/` generates the core, runs the characterization on it and writes `timing/<tag>.json`, which the stand-in picks up (docs/CHARACTERIZATION.md). `--xci path/to/your.xci` takes the configuration of an existing IP instead.

## Known limitations

The stand-in follows the core's handshakes clock for clock in the usual traffic: frames back to back or with idle clocks between them, input gaps, output stalls of any length, a blocked output and its release. Where it is known to differ:

- **Overflow.** After an overflow the C model's output data is not the core's. The `OVFLO` flag is exact (set on every output sample of an overflowing frame, and in its status word). A test that overflows on purpose should check the flag only.
- **Event outputs** are not cycle-exact: `event_data_in_channel_halt` comes 2 clocks late, `event_frame_started` of a frame that starts with less than the full input slack 1 clock late, `event_data_out_channel_halt` is approximate, and `event_status_channel_halt` stays low.
- **Status channel backpressure.** With `m_axis_status_tready` low the stand-in queues the status words and never stalls; the core's behaviour in that case is not reproduced.
- **Input gaps with the output blocked and released.** Input gaps while the input FIFO fills behind a blocked output are modelled: the stand-in refuses input where the core does, also for dense and clustered gap patterns (every gap pattern measured on the real core, `tests/engine_c/`). When the output is blocked and released again while input gaps arrive, the input can resume up to a few clocks (mostly 2) earlier than on the core in some patterns, and then the output and status words by as much; an output block that holds a frame's end can shift the input by many clocks. Data is not affected. `tests/reference/known_differences.json` lists the measured cases.
- **Dense 1-clock output stalls** can shift input handshakes by a few clocks (up to about 9 measured).
- **`event_frame_started` of the real core on Riviera-PRO** reads one clock early in the tests' cocotb testbench (it samples the port right after the clock edge, where Riviera already shows the new value of this output): the real core's flow test on Riviera fails on that check. The stand-in is not affected (its traces on Riviera equal XSim's).
- **Slow input.** With input gaps of 2 clocks or more before every sample (a third of the clock rate or less), the average rate and the end of the frame agree, but single output samples can come up to 2 clocks apart from the core's.
- **Supported cores only.** Other architectures (radix-4 or radix-2 burst I/O), real-time throttling, run-time configurable transform length, unscaled or block floating-point output, cyclic prefix insertion, bit-reversed output and xfft versions other than 9.x are not supported; `model/xfft_config.py` and the `.xci` reader refuse them.
- **No Versal.** On Versal parts the real core's XSim simulation outputs all-zero data for any input, so nothing can be characterized there: `python -m characterize` (and the API, and `--xci`) stops with exit code 2 (`CharacterizeError`) before it generates a core. Supported families: 7-series, Zynq UltraScale+, Virtex UltraScale+.
- **Timing constants per configuration.** A configuration without constants stops the simulation at time 0 (the stand-in's `xfft_engine_new` refuses it). The characterization needs 256 points or more.

The stand-in is for speed. Sign-off, and anything it does not model, uses the real core: the same tests run on both (`FFT_MODEL=rtl`).

## Documentation

| Document | What |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | integration into your testbench on XSim, Riviera-PRO, NVC, GHDL and Verilator, cocotb, parameters, config channel, run-time behaviour, troubleshooting |
| [docs/INTERFACE.md](docs/INTERFACE.md) | reference: the wrappers, their ports and package, the C engine's functions, the generated files |
| [docs/CHARACTERIZATION.md](docs/CHARACTERIZATION.md) | measuring the timing constants of a configuration: CLI, Python API, `.xci`, the JSON |
| [docs/CACHE.md](docs/CACHE.md) | the result cache and the elaboration cache: directory and S3 stores, snapshot portability |
| [examples/README.md](examples/README.md) | the examples and how to run each |

## Layout

| Path | What |
|---|---|
| `ip/xfft_core.tcl` | Tcl procedure that creates the core from parameters (newest xfft 9.x of the installation) |
| `model/xfft_engine.c`, `model/xfft_engine.h` | the stand-in: the C engine, all of its timing, with `xfft_dpi.c` in one library |
| `model/xfft_engine.sv`, `model/xfft_engine.vhdl` | the C engine's thin wrappers with the core's module name and ports: SystemVerilog (DPI-C, XSim, Riviera-PRO and Verilator) and VHDL-2008 (VHPIDIRECT, NVC and GHDL) |
| `model/xfft_dpi.c` | shim around the C model, called by the engine |
| `model/xfft_dpi_stub.c` | the same functions without the C model, for the timing-only build (`build_engine.py --timing-only`) |
| `model/xfft_config.py` | the configuration: defaults, `XFFT_CONFIG`, port widths, the builder Tcl |
| `model/xfft_timing.py` | the timing constants (`TABLE`, `timing/`), the timing rules, the generators of the SV and VHDL packages and the C header |
| `model/cmodel_archive.py` | finds the C model in the generated core and reads the names in it |
| `timing/` | characterization results (`<tag>.json`) the stand-in uses |
| `scripts/generate_ip.py` | generates the core and its C model with Vivado |
| `scripts/build_engine.py` | builds the C engine's library, its SV and VHDL packages and C header |
| `scripts/read_xci.tcl` | reads the configuration of an `.xci` with Vivado (for the characterization) |
| `scripts/check_docs.py` | checks the ASCII diagrams and tables of the Markdown files |
| `characterize/` | the characterization: CLI (`python -m characterize`), API, `.xci` reader, the bench in `bench/` |
| `tests/` | cocotb runner, source lists, result and elaboration caches, test bench parts, the tests of the core |
| `tests/timing/` | timing bench in plain XSim: validate, measure, probe, speed |
| `tests/unit/` | tests that need no Vivado |
| `tests/reference/` | the real core's timing in the characterization cases of 949 configurations, for `tests/test_timing_reference.py` |
| `examples/` | examples, each runnable with one command |
| `VERSION`, `CHANGELOG.md` | the version (0.2.0) and the changes |

## Licence

MIT, see [LICENSE](LICENSE). AMD's C model, the IP and its simulation files are not part of this repository and stay under AMD's licence terms; they come from your Vivado installation.
