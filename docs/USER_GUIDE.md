# User guide

The stand-in for the generated xfft core is the C engine (`model/xfft_engine.c`, `FFT_MODEL=engine`): the core's cycle timing in C, one step per rising edge of `aclk`, its state in a handle. Two thin wrappers give it the core's module name and ports and make no timing decisions of their own: `model/xfft_engine.sv` (SystemVerilog, DPI-C: XSim, Riviera-PRO and Verilator) and `model/xfft_engine.vhdl` (VHDL-2008, VHPIDIRECT: NVC and GHDL, the same file for both). The engine computes every frame with AMD's bit-accurate C model of the same core (through `model/xfft_dpi.c`, compiled into the same library) and drives the AXI4-Stream handshakes with the core's cycle timing. The real core runs in XSim, and in Riviera-PRO with AMD's simulation libraries compiled for it; the characterization runs in XSim only. This guide shows how to put the stand-in into a testbench, what it expects and does at run time, and what to do when something goes wrong. The SV, VHDL and C details are in [INTERFACE.md](INTERFACE.md).

## Setup

Once per shell:

```sh
source /path/to/Vivado/<version>/settings64.sh       # vivado, xvlog, xvhdl, xelab, xsim on PATH
source .venv/bin/activate                            # python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The C engine is built with `gcc` (or the compiler `CC` names). The repository is tested with Vivado 2025.2 (xfft 9.1). With another Vivado the tools work the same and print a warning when they generate a core; measure the timing constants of your configuration with it ([CHARACTERIZATION.md](CHARACTERIZATION.md)).

## The configuration

Everything follows from one configuration (`model/xfft_config.py`): the core Vivado generates, the C model's parameters, the stand-in's port widths, the timing constants and the build directories. The default is a 1024-point core with 16-bit data and phase factors, convergent rounding, part `xc7a35tcpg236-1`, 100 MHz. `XFFT_CONFIG` overrides fields of it for every tool and test:

```sh
export XFFT_CONFIG=nfft=8                                   # 256 points, otherwise the default
export XFFT_CONFIG=nfft=12,input_width=18,twiddle_width=18,rounding=truncation,part=xczu4eg-sfvc784-1-e
```

| Field | Values | Meaning |
|---|---|---|
| `nfft` | 3 .. 16 | log2 of the transform length N (8 to 65536 points) |
| `input_width` | 8 .. 34 | data bits per component; the output has the same width (fixed scaling) |
| `twiddle_width` | 8 .. 34 | phase factor bits |
| `rounding` | `convergent`, `truncation` | the core's rounding mode |
| `part` | an installed part | the device the core is generated for |
| `clock_mhz` | whole MHz | the IP's target clock frequency (it can change the core's pipeline, so its latency) |
| `module` | a module name | the core's name; the stand-in compiles under the same name, and its package, library and C functions are named after it ([several configurations in one simulation](#several-configurations-in-one-simulation)) |

The configuration's tag (`python -c "import sys; sys.path.insert(0, 'model'); import xfft_config; print(xfft_config.current().tag)"`) names its build directory `build/<tag>/` and its timing constants.

The core's architecture is fixed: pipelined streaming I/O, fixed point, scaled, natural-order output, non-real-time throttling, `XK_INDEX` and `OVFLO` in `m_axis_data_tuser`, `aresetn`, no `aclken`. A core with other options is not supported.

### Timing constants

The stand-in takes its timing from 16 constants per configuration (`NAMES` in `model/xfft_timing.py`; their meaning is in that module's docstring and in [CHARACTERIZATION.md](CHARACTERIZATION.md)). `model/xfft_timing.constants()` looks for them, in this order:

1. `TABLE` in `model/xfft_timing.py`: the default configuration ships there;
2. `timing/<tag>.json`: a characterization result (`python -m characterize ... --out timing/`); the 256-point and a 512-point configuration ship there;
3. `build/<tag>/timing_measured.json`: written by `python tests/timing/xfft_timing_bench.py measure`.

Without constants the stand-in stops at time 0: `xfft_engine: xfft_model.h has no timing constants for this configuration`, then `xfft_engine_new failed`.

## The stand-in in your own XSim testbench

The stand-in needs a generated SV package (`xfft_model_pkg`: widths and timing constants) and the C engine's library (`libxfft_engine.so`: the engine with the configuration's constants compiled in, and the C model). `scripts/build_engine.py` builds both for the configuration of `XFFT_CONFIG`, generating the core with Vivado the first time:

```sh
export XFFT_CONFIG=nfft=8
python scripts/build_engine.py build/my_engine
```

Then compile the package and the SV wrapper with your sources, and elaborate with the library:

```sh
mkdir -p build/my_sim && cd build/my_sim
xvlog -sv -work xil_defaultlib -d XFFT_CORE_NAME=xfft_core \
    ../my_engine/xfft_model_pkg.sv ../../model/xfft_engine.sv <your Verilog/SV sources>
xvhdl -work xil_defaultlib <your VHDL sources>                     # if any
xelab -L xil_defaultlib -sv_root ../my_engine -sv_lib libxfft_engine -snapshot top xil_defaultlib.<your top>
xsim top -R
```

Notes:

- The module's name is `` `XFFT_CORE_NAME `` (default `xfft_core`). Set it to the name your design instantiates (`-d XFFT_CORE_NAME=<name>`), or set the configuration's `module` field, which also names the generated core. With a `module` other than `xfft_core` every name follows it: compile `<dir>/<module>_pkg.sv` and the wrapper copy `<dir>/<module>.sv` (instead of `model/xfft_engine.sv`) and elaborate with `-sv_lib lib<module>_engine` ([several configurations in one simulation](#several-configurations-in-one-simulation)).
- Keep the library's directory apart from the elaboration directory. `xelab` links the library by its absolute path when `-sv_root` names another directory, so the snapshot runs from anywhere; with the elaboration directory itself it would link `./libxfft_engine.so`. The cocotb runner instead builds it into a subdirectory `dpi/` of the elaboration directory: the snapshot then links `./dpi/libxfft_engine.so` and can be moved or copied with the directory, which the elaboration cache relies on ([CACHE.md](CACHE.md#snapshot-portability)).
- Compile the package first: the wrapper imports `xfft_model_pkg` (`<module>_pkg` for another module).
- The C model's own libraries are found through the library's rpath; no `LD_LIBRARY_PATH` is needed. No `glbl` is needed for the stand-in either.
- A VHDL design instantiates the stand-in as it would the core (component or entity instantiation of `xfft_core`): XSim binds the VHDL instance to the SystemVerilog module of that name.
- The real core and the stand-in can be in the same simulation: compile the stand-in under another name with `-d XFFT_ENGINE_NAME=<other name>` (`tests/xfft_pair.sv` does this).
- `build_engine.py` rebuilds the library only when its sources, the constants or the C model change; rerunning it is cheap. Rerun it whenever the configuration or its constants change.

`examples/plain_xsim/` is a complete example of these steps, and `tests/timing/` is a larger plain-XSim bench.

## The stand-in in NVC, GHDL and Verilator

The same library serves the VHDL wrapper on NVC and GHDL and the SV wrapper on Verilator. `build_engine.py` also writes a VHDL package `xfft_model_pkg` with the same constants. It needs the C model of the generated core: the first build of a configuration generates the core with Vivado (`scripts/generate_ip.py`); after that, NVC, GHDL and Verilator need no Vivado, only the `build/<tag>/ip_gen/` it left (copy it to a machine without Vivado).

In a VHDL testbench of your own, without cocotb:

```sh
export XFFT_CONFIG=nfft=8
python scripts/build_engine.py build/my_engine
# NVC: the library preloaded, so that NVC finds the VHPIDIRECT functions in the process
nvc --std=2008 -a build/my_engine/xfft_model_pkg.vhdl model/xfft_engine.vhdl <your VHDL sources>
LD_PRELOAD=$PWD/build/my_engine/libxfft_engine.so nvc --std=2008 -e <your top> -r
# GHDL (LLVM or GCC backend): the library linked into the executable
ghdl -a --std=08 build/my_engine/xfft_model_pkg.vhdl model/xfft_engine.vhdl <your VHDL sources>
ghdl -e --std=08 -Wl,$PWD/build/my_engine/libxfft_engine.so -Wl,-Wl,-rpath,$PWD/build/my_engine \
    -Wl,-Wl,-rpath-link,$PWD/build/my_engine/cmodel <your top>
./<your top>
```

In Verilator (tested with 5.036) the SV wrapper, the library linked into the executable, with `--binary` for a testbench in SystemVerilog or `--cc --exe` with a C++ main of your own:

```sh
E=$PWD/build/my_engine
verilator --binary --timing --timescale 1ns/1ps --top-module <your top> \
    $E/xfft_model_pkg.sv model/xfft_engine.sv <your sources> $E/libxfft_engine.so \
    -LDFLAGS "-Wl,-rpath,$E -Wl,-rpath-link,$E/cmodel"
obj_dir/V<your top>
```

Notes:

- VHDL-2008 only: the C functions take 32-bit integers, and NVC's VHDL-2019 `integer` is 64 bits wide; the wrapper stops at time 0 then. When the configuration's `module` is not `xfft_core`, `build_engine.py` writes copies of the wrappers with that module's names into `<dir>/<module>.vhdl` and `<dir>/<module>.sv`, beside its package `<dir>/<module>_pkg.vhdl` and library `<dir>/lib<module>_engine.so`; compile and load those instead.
- One library per configuration: `xfft_engine_new()` refuses a library built for another configuration than the package the HDL was compiled with. Configurations with different modules can share one simulation, each with its own library ([below](#several-configurations-in-one-simulation)).
- `libxfft_engine.so` exports only its four functions `xfft_engine_*` (`-fvisibility=hidden`, `-Bsymbolic`; `<module>_engine_*` for another module). AMD's prebuilt C model exports its own global names into the process; a testbench that loads another library with the same names (e.g. another GMP) can clash with it.
- The edge: the SV wrapper steps on `posedge aclk`, the VHDL wrapper on `rising_edge(aclk)`. They differ only on a clock that starts at 1 or passes through X: Verilog counts x to 1 as an edge, VHDL does not.
- Unknown inputs (X, Z, and in VHDL U, W, -): an unknown `aresetn` resets nothing, an unknown `tvalid` or `tready` makes no handshake (and no stall), an unknown `s_axis_data_tlast` raises neither tlast event; unknown bits of the data and config words are 0. VHDL `H` and `L` are 1 and 0.
- Verilator: single-threaded only. The engine keeps its table of engines (and AMD's C model its own state) in global variables without a lock, so do not build with `--threads` (cocotb's runner does not). Verilator is 2-state: before the first edge the outputs are 0 where the other simulators have X, the X and Z handling above does not apply, and a clock that starts at 0 has no falling edge at time 0. Its lint warnings are fatal by default (`-Wno-fatal` relaxes that for sources of your own); the wrapper builds without warnings. Verilator needs a C++ compiler and `make`, and uses `ccache` when it was built with it.
- GHDL with cocotb ends the simulation at once when the test writes nothing at time 0: start the clock or set a port at time 0, as the tests do.
- Speed: in a plain HDL testbench (stimulus in memory, CPU time, 1024 points, 3.4 M clocks) NVC runs the stand-in 2.1 times and GHDL 1.2 times as fast as XSim, or 2.5 and 1.8 times when the testbench's own cost is left out. Under cocotb the Python side's cost differs per simulator: `tests/test_xfft_trace.py` at 1024 points (950 000 clocks) took 270 s on XSim, 102 s on NVC and 105 s on GHDL, about 2.7 times as fast. NVC and GHDL build in a second.

## Several configurations in one simulation

A design with cores of different configurations (say a 256-point and a 512-point core) simulates each with its own stand-in. Give every configuration its own `module`: all names of its stand-in follow from it, so that the libraries, packages and wrappers of the configurations do not collide.

| Name | module `xfft_core` | any other module `<module>` |
|---|---|---|
| SV module, VHDL entity | `xfft_core` (in SV the macro `XFFT_CORE_NAME`) | `<module>` |
| package (SV and VHDL), its files | `xfft_model_pkg`, `<dir>/xfft_model_pkg.sv`, `.vhdl` | `<module>_pkg`, `<dir>/<module>_pkg.sv`, `.vhdl` |
| wrappers | `model/xfft_engine.sv`, `model/xfft_engine.vhdl` | the copies `<dir>/<module>.sv`, `<dir>/<module>.vhdl` |
| library | `<dir>/libxfft_engine.so` | `<dir>/lib<module>_engine.so` |
| C functions | `xfft_engine_new`, `_step`, `_free`, `_wallclock` | `<module>_engine_new`, `_step`, `_free`, `_wallclock` |

`xfft_config.XfftConfig` has them as `module`, `model_package` and `engine_prefix`. A module is a VHDL basic identifier (a letter, then letters and digits, single underscores between them). The modules `xfft`, `xfft_model` and `xfft_engine`, and any that differ from `xfft_core` or from them only in case, are refused: their names would be the default's (VHDL names do not depend on case). For the same reason, two modules in one VHDL simulation must differ in more than case. Two instances of one configuration need nothing of this: they share one library, each instance with its own engine.

Build every configuration into its own directory, with its own `XFFT_CONFIG`. The module's name does not change the timing: a configuration whose module is not `xfft_core` has its own tag, ending in `_<module>`, and takes the constants found under that tag, else those of the same configuration with module `xfft_core` (`TABLE`, `timing/`, the measured file), so the constants that ship apply under any module:

```sh
python scripts/build_engine.py build/fft_a                    # module xfft_core, 1024 points
XFFT_CONFIG=nfft=9,input_width=18,twiddle_width=18,rounding=truncation,clock_mhz=250,module=xfft_b \
    python scripts/build_engine.py build/fft_b
```

Then compile every package and wrapper (in one library: their names differ) and load or link every library:

| Simulator | Compile | Load or link the libraries |
|---|---|---|
| XSim | `xvlog -sv build/fft_a/xfft_model_pkg.sv model/xfft_engine.sv build/fft_b/xfft_b_pkg.sv build/fft_b/xfft_b.sv ...` | `xelab ... -sv_root build/fft_a -sv_lib libxfft_engine -sv_root build/fft_b -sv_lib libxfft_b_engine` |
| Riviera-PRO | `alog -sv -timescale 1ns/1ps` with the same files | `asim -sv_lib build/fft_a/libxfft_engine -sv_lib build/fft_b/libxfft_b_engine ...` |
| Verilator | the same files | both libraries as file arguments, `-LDFLAGS "-Wl,-rpath,<dir> -Wl,-rpath-link,<dir>/cmodel"` for each directory |
| NVC | `nvc --std=2008 -a build/fft_a/xfft_model_pkg.vhdl model/xfft_engine.vhdl build/fft_b/xfft_b_pkg.vhdl build/fft_b/xfft_b.vhdl ...` | `LD_PRELOAD=<abs>/build/fft_a/libxfft_engine.so:<abs>/build/fft_b/libxfft_b_engine.so nvc ... -r` |
| GHDL | `ghdl -a --std=08` with the same files | `ghdl -e --std=08 -Wl,<lib> -Wl,-Wl,-rpath,<dir> -Wl,-Wl,-rpath-link,<dir>/cmodel` for each library and its directory |

Notes:

- Do not set `XFFT_ENGINE_NAME` or `XFFT_CORE_NAME` for the whole compilation when several stand-ins are in it: `model/xfft_engine.sv` takes its module name from them, and the copies for other modules take `` `XFFT_ENGINE_NAME `` (only that one) over their own. The wrappers only read these macros; they define none, so nothing leaks from one wrapper into the files after it.
- A testbench that imports a package names it: `` import xfft_b_pkg::*; `` or, for two configurations side by side, qualified names (`xfft_model_pkg::XFFT_DATA_WIDTH`, `work.xfft_b_pkg.XFFT_DATA_WIDTH`). `tests/timing/tb_xfft_timing.sv`, `tests/xfft_pair.sv` and `examples/plain_xsim/tb_tone.sv` take the package's name from `` `XFFT_MODEL_PKG `` (default `xfft_model_pkg`), and `tests/timing/tb_xfft_timing.sv` the C name of `xfft_engine_wallclock` from `` `XFFT_ENGINE_WALLCLOCK `` (default `xfft_engine_wallclock`). A testbench of your own that uses `` `XFFT_CORE_NAME `` defines its own default (`` `ifndef XFFT_CORE_NAME `` ..., as `tb_tone.sv` does): the wrapper no longer defines it.
- With the cocotb infrastructure, pass one stand-in per configuration: `hdl_sources.xfft_engine(cfg, timing_only=..., constants=...)` builds for `cfg` whatever `XFFT_CONFIG` the test process has (its build gets `XFFT_CONFIG=cfg.spec` and, when given, `XFFT_TIMING_JSON=constants`), and `sim_runner.run(sources=[model_a, model_b, *your_sources], ...)` builds each into `dpi/<prefix>/` (its C prefix, e.g. `dpi/xfft_b_engine/`) and loads or links all of them on every simulator. `tests/test_xfft_multi.py` does this: two instances of one configuration and one of another in one simulation, on every installed simulator, each instance's handshakes compared with the same traffic on its configuration alone. With `XFFT_MULTI_CMODEL=1` it builds the stand-ins with the C model and sends random data, so the output words are compared too, and each output frame is checked against a floating-point FFT; `XFFT_MULTI_A` and `XFFT_MULTI_B` (`XFFT_CONFIG` syntax) replace its two configurations.

## The stand-in and the real core in Riviera-PRO

Riviera-PRO (tested with 2026.04) runs the SV wrapper with the same library as XSim: compile the generated package and `model/xfft_engine.sv` as SystemVerilog with the time unit of your testbench (`alog -sv -timescale 1ns/1ps`; the sources have no `` `timescale `` of their own), and load the library at elaboration with `-sv_lib` and its path without the `.so` suffix:

```sh
python scripts/build_engine.py build/dpi
alog -sv -timescale 1ns/1ps build/dpi/xfft_model_pkg.sv model/xfft_engine.sv <your testbench>
asim -sv_lib build/dpi/libxfft_engine <your top>
```

No other link step: Riviera's simulation process loads the library itself. The library finds AMD's C model in `cmodel/` beside it through its own `DT_RPATH`, which also wins over the `LD_LIBRARY_PATH` that Riviera's launcher sets. Riviera simulates in one thread, which the engine needs (its table of engines has no lock).

The real core on Riviera needs AMD's simulation libraries compiled for Riviera once, with Vivado (`compile_simlib -simulator riviera -directory <dir>`); name that directory in `XFFT_RIVIERA_LIBS`. `tests/riviera_ip.py` then exports the generated core's simulation sources for Riviera (`export_simulation -simulator riviera`, about 30 s, kept in the build directory) and maps the precompiled libraries; without `XFFT_RIVIERA_LIBS` the tests of the real core skip Riviera with that reason. Riviera decrypts the core's encrypted simulation model.

The tests find Riviera by `vsimsa` on PATH (checked to be Riviera-PRO, not another `vsim`). Run Riviera tests one simulation at a time (no `pytest -n`): one license runs one simulation.

## The stand-in with cocotb

The test infrastructure in `tests/` runs cocotb tests on either model: the real core on XSim through cocotb-vivado, the stand-in on XSim, NVC, GHDL and Verilator:

```python
import hdl_sources, sim_runner, xfft_config

cfg = xfft_config.current()
model = hdl_sources.fft_model()                     # FFT_MODEL: rtl (default) or engine
sim_runner.run(
    sources=[hdl_sources.xfft_core(cfg, model), *your_sources],
    hdl_toplevel="your_top",
    hdl_toplevel_lang="verilog",                    # "vhdl" for a VHDL toplevel
    test_module=__name__,
    build_name=f"{cfg.tag}/your_test",
    verilog_defines={"XFFT_CORE_NAME": cfg.module},
    extra_env={"FFT_MODEL": model, "XFFT_CONFIG": os.environ.get("XFFT_CONFIG", "")},
    cache=True,                                     # the result cache (docs/CACHE.md)
)
```

`hdl_sources.xfft_core()` gives the generated core (`FFT_MODEL=rtl`, cocotb-vivado generates it from the same builder Tcl) or the stand-in (`FFT_MODEL=engine`, built by `build_engine.py` into the build directory). The same test then runs on both: the stand-in for speed, the real core for sign-off. When the core itself is the toplevel, pass `hdl_toplevel_lang=hdl_sources.toplevel_lang(model, sim)` (the IP's simulation wrapper is VHDL; the stand-in's wrapper is SystemVerilog on XSim and Verilator and VHDL on NVC and GHDL).

With `FFT_MODEL=engine` the tests that run the core as the toplevel run on every simulator that is installed (`hdl_sources.simulators()`; the others are skipped):

```sh
FFT_MODEL=engine pytest tests/test_xfft_flow.py examples/        # XSim, Riviera, NVC, GHDL and Verilator, where installed
```

In `sim_runner.run(sim=..., ...)` the stand-in (`hdl_sources.xfft_engine(cfg)`) becomes its SV sources and the `-sv_lib` on XSim, its VHDL sources and the library on NVC (preloaded, `LD_PRELOAD`) and on GHDL (linked into the executable), its SV sources and the library on Verilator (linked into the executable that cocotb's Verilator runner builds, with its rpath). The stand-in on Riviera-PRO is its SV sources and `-sv_lib` (asim). The real core (`rtl`) runs on XSim and Riviera-PRO (with `XFFT_RIVIERA_LIBS`); on NVC, GHDL and Verilator its tests are skipped with the reason.

`tests/xfft_tb.py` has reusable parts: `Frame` (data, direction, scaling schedule, input gaps, output stalls), `Lane` (drives a core's config and data inputs and its output `tready` and records every handshake with its clock), `random_frame()`, `reference()` (numpy with the core's scaling) and `check_frame()`. `examples/cocotb_backpressure/` and `examples/config_channel/` use them.

Switches: `FFT_MODEL` (`rtl`, `engine`), `XFFT_CONFIG`, `WAVES=1` (a waveform database; bypasses the cache), `COCOTB_RANDOM_SEED`, `SIM_CACHE` and `SIM_CACHE_URL` ([CACHE.md](CACHE.md)).

`pytest tests/test_xfft_trace.py` checks that the wrappers give the same behaviour on every simulator: the random traffic of `tests/xfft_chaos.py` (mid-frame resets, status channel stalls, config words on every clock, tlast errors, output blocks that fill the input FIFO and every result slot, idle runs, X and Z on single inputs) and every case of the characterization on the stand-in on each simulator, one trace line per clock with every input and output; XSim's traces are the reference, and the comparison names the first difference. Each run also checks the traffic on its own: it runs to the end with output and status handshakes, and no output is unknown from the first edge on. Run it where XSim is first (it writes the reference traces), then where NVC, GHDL and Verilator are, with the same `build/`. The clock starts the same way on every simulator (low until the inputs are driven at 1 ns) and nothing before the first edge is compared. Verilator, being 2-state, runs the traffic without X and Z only (`trace_2state.txt`; the other simulators write it too, beside `trace.txt` with X and Z) and the characterization cases. A trace whose stand-in has changed since it was written is left out of the comparison (its `model.sha256`).

## Ports and data formats

The ports are the core's ([INTERFACE.md](INTERFACE.md) lists them with their widths):

| Channel | Format |
|---|---|
| `s_axis_config_tdata` | bit 0 `FWD_INV` (1 forward, 0 inverse), above it `SCALE_SCH`: 2 bits per radix-4 stage, stage 0 lowest; padded to whole bytes |
| `s_axis_data_tdata` | imaginary part in the upper half, real part in the lower half, each `input_width` bits two's complement in a field padded to whole bytes |
| `m_axis_data_tdata` | as the input; each field sign-extended to its padded width |
| `m_axis_data_tuser` | `XK_INDEX` (the output index, natural order) in the low byte(s), `OVFLO` in bit 0 of the byte above |
| `m_axis_status_tdata` | one word per frame: bit 0 `OVFLO` of the frame |

`SCALE_SCH` has `ceil(nfft / 2)` stages; with an odd `nfft` the last stage is radix 2 and takes 0 or 1. The total shift divides the result: a forward transform with `SCALE_SCH` 2 in every stage (1 in a radix-2 last stage) divides by about N, which cannot overflow. The core's inverse transform is unnormalised (N times numpy's `ifft`). `XfftConfig.config_word(forward, schedule)` builds the word.

## Run-time behaviour

- **Reset.** `aresetn` is sampled at the clock. Both input `tready` rise `RESET_TO_READY` clocks after the first clock with `aresetn` high (3 on the shipped configurations). After reset the configuration is forward with `SCALE_SCH` 2 in every stage (1 in a radix-2 last stage).
- **Config channel.** A one-word buffer: `s_axis_config_tready` is high while it is empty. A word is taken by the next frame when that frame's first sample is accepted; a frame without a new word uses the last one. Offer the word of a frame before (or with) its first sample.
- **Frames.** A frame is N accepted input samples. `s_axis_data_tlast` only drives `event_tlast_unexpected` and `event_tlast_missing`, as in the core; it does not end a frame. Frames can follow each other back to back without a gap (`FRAME_BOUNDARY_STALL` = 0).
- **Latency.** With `tvalid` and `tready` high throughout, the first output sample comes `LATENCY_FIRST_OUT` clocks after the frame's first input handshake (2182 for the default 1024-point core), then one sample per clock in natural order, `tlast` on sample N - 1.
- **Input gaps.** A lone 1-clock gap is free (the input slack, `INPUT_SLACK` = 1); longer gaps, or a second gap before the slack refills, delay the frame by their cost. In non-real-time mode a gap's cost also holds up the output of earlier frames.
- **Output stalls.** Each clock of `m_axis_data_tready` low delays the output by one clock. Up to `OUTPUT_HALT_EVENT_AFTER` samples (14) queue behind the output register; a longer stall pauses the core's pipeline, which then takes input only until its input buffer is full (`INPUT_LIMIT` samples with the output blocked from the start). When the output moves again, input resumes after `INPUT_RESUME` samples have left the buffer.
- **Status.** One status word per frame, a few clocks after the frame's last output (`STATUS_AFTER_LAST_OUT`), later after long stalls.
- **Overflow.** The C model gives one overflow flag per frame; the stand-in puts it on every output sample of that frame and into its status word, as the core does for an overflowing frame. The data of an overflowing frame differs from the core's.
- **Events.** `event_frame_started` pulses `FRAME_STARTED_DELAY` clocks after a frame's first input handshake; `event_fft_overflow` follows each output handshake with `OVFLO`; `event_tlast_*` as above; `event_data_in_channel_halt` and `event_data_out_channel_halt` mark starving input and a paused pipeline with stalled output. The events are not cycle-exact (see below); `event_status_channel_halt` stays low.

All the timing rules, clock by clock, are in the docstring of `model/xfft_timing.py`, which also has functions that predict latencies (`gap_cost()`, `status_delay()`, ...); the tests use them.

## Checking a configuration against the real core

- `python -m characterize` measures a configuration's constants and compares the stand-in built from them with the real core on all its cases ([CHARACTERIZATION.md](CHARACTERIZATION.md)).
- `pytest tests/test_xfft_compare.py`: the real core and the stand-in side by side in one simulation, 8 frames (random data, both directions, two scaling schedules, an overflow, random gaps and stalls), word by word and clock by clock.
- `FFT_MODEL=rtl pytest tests/test_xfft_flow.py` and `FFT_MODEL=engine ...`: the same flow-control tests on each model, the latencies checked against the constants.
- `pytest tests/test_timing_reference.py`: the stand-in against the real core's timing stored in `tests/reference/` (949 configurations, 8 to 65536 points, every width, rounding and family measured; each with the characterization cases measured on it), with no Vivado. By default it runs the checked configurations (those in `known_differences.json`); `XFFT_TIMING_TAGS=all` runs every one, `XFFT_TIMING_TAGS='n8192_*,n512_*'` the matching ones. The 115 configurations with enough cases measured carry their timing constants and run; the others (mostly two cases each) are kept in the reference as measured data and are skipped. The engine is built timing-only (`python scripts/build_engine.py --timing-only <dir>`, or `XFFT_TIMING_ONLY=1`): `model/xfft_dpi_stub.c` takes the place of AMD's C model, every output sample is 0, the clocks are the same. It runs on every installed simulator and compares the clocks of tready and of every handshake; data is not compared. `tests/reference/known_differences.json` lists the few known differences with their reason. CI runs it on NVC and GHDL; `XFFT_TIMING_CASES=a,b` runs only those cases.
- The timing bench in plain XSim, one process per case, all in parallel (`-j` limits them):

  ```sh
  python tests/timing/xfft_timing_bench.py validate -j 2              # the constants against the real core
  python tests/timing/xfft_timing_bench.py validate --model engine     # the stand-in against its constants
  python tests/timing/xfft_timing_bench.py measure -j 2               # derive the constants (build/<tag>/timing_measured.json)
  python tests/timing/xfft_timing_bench.py probe -j 2 g256:1,g512:5 s300:4    # any gap/stall pattern, latencies
  python tests/timing/xfft_timing_bench.py speed --model engine --frames 2000  # simulated clocks per second
  ```

  `probe` patterns: `g<sample>:<clocks>` an input gap before an input sample, `s<sample>:<clocks>` an output stall before an output sample, up to 4 of each; positions count over all frames (`--frames`).

## Known limitations

- After an overflow the output data differs from the core's; the `OVFLO` flag is exact.
- The event outputs are not cycle-exact: `event_data_in_channel_halt` comes 2 clocks late, `event_frame_started` of a frame that starts with less than the full input slack 1 clock late, `event_data_out_channel_halt` is approximate, `event_status_channel_halt` stays low.
- `m_axis_status_tready` low: the stand-in queues the status words and never stalls the core; the core's own behaviour then is not reproduced.
- Input gaps while a long output stall pauses the pipeline: the core can refuse input a few samples earlier than the stand-in. Input handshakes then differ by a few clocks, more at large transform lengths, and in some patterns the output and status words by 1 to 3 clocks. The characterization's `dense_backpressure` case is such a pattern and is compared with a tolerance.
- Dense 1-clock output stalls together with input gaps can shift input handshakes by a few clocks at 8192 points and more.
- Input at a third of the clock rate or less (2 or more idle clocks before every sample): the rate and the frame's end agree, single output samples can be up to 2 clocks off.
- 64- and 128-point cores: the stand-in runs them when it has constants, but the characterization cannot measure them (it needs 256 points or more).
- Other architectures, real-time throttling, run-time transform length, unscaled or block floating-point output, cyclic prefix and xfft versions other than 9.x are not supported.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `xfft_engine: xfft_model.h has no timing constants for this configuration` | No constants for the tag: characterize it (`python -m characterize ... --out timing/`) or check `XFFT_CONFIG` against the shipped tags |
| `xfft_engine: xfft_model.h does not fit this model (...)` | The constants break an assumption of the stand-in (listed in the message); the core behaves in a way the stand-in cannot express |
| `xfft_engine: the HDL's configuration (...) is not the one this library was built for` | the package and the library are of different configurations: rerun `build_engine.py` with the `XFFT_CONFIG` of the HDL build. Two configurations in one simulation need different modules (each its own library, [above](#several-configurations-in-one-simulation)) |
| `FFT_MODEL=cmodel is no longer supported` | the stand-in is `FFT_MODEL=engine` |
| `no timing constants for <tag>` (Python) | as above, from the tests or the timing bench |
| `vivado failed (1), see build/<tag>/ip_gen/vivado.log` | the IP could not be generated: the part or the xfft IP is not installed, or a parameter is out of range; the log names it |
| `the xfft IP of this Vivado has no cmodel output product` | the installation's xfft has no C model; the stand-in needs it |
| `expected one *bitacc_cmodel_lin64.zip` | the core was generated without its C model; delete `build/<tag>/ip_gen/` and rerun |
| `model/xfft_dpi.c is written for the xfft 9.x C model` | the installation's xfft is not 9.x |
| `build_engine: gcc failed (...), see <dir>/cc.log` | the engine did not compile against the C model; the log has the compiler command and output |
| `build_engine: C compiler gcc not found (set CC)` | install `gcc`, or name another compiler in `CC` |
| the snapshot cannot find `libxfft_engine.so` | `-sv_root` was the elaboration directory: build the library in a directory of its own |
| cocotb: cannot load libpython | install the shared Python library (`python3-dev`, or `libpython3.X`) |
| `xelab not on PATH` (tests skipped) | `settings64.sh` not sourced |
| a warning `Vivado <x>; this repository is tested with Vivado 2025.2` | informational: measure the constants of your configuration with this Vivado |
| data differs from the core in one frame | did the frame overflow (`OVFLO`)? Then only the flag is exact |
| a handshake differs from the core | see "Known limitations"; otherwise report it with the issue template (`.github/ISSUE_TEMPLATE/`), with the configuration and the traffic pattern |

After changing `XFFT_CONFIG` nothing needs cleaning: every configuration builds in its own `build/<tag>/`. `build/` can be deleted at any time; the next run regenerates what it needs.
