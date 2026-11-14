# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-14

### Added

- Several configurations in one simulation, on every simulator: the stand-in's names follow the configuration's
  `module`. For a module other than `xfft_core` the library is `lib<module>_engine.so` and exports
  `<module>_engine_new`, `_step`, `_free` and `_wallclock`, the package is `<module>_pkg`, and
  `scripts/build_engine.py` writes copies of both wrappers with these names (`<dir>/<module>.sv`,
  `<dir>/<module>.vhdl`). Module `xfft_core` keeps every name of 0.1.0. docs/USER_GUIDE.md, "Several
  configurations in one simulation".
- Test infrastructure: `hdl_sources.xfft_engine(cfg, timing_only, constants)` builds for its own configuration
  (and constants), whatever `XFFT_CONFIG` the test process has; `sim_runner.run()` builds every stand-in among its
  sources into its own directory and loads or links all their libraries (XSim and Riviera-PRO: one `-sv_lib` each;
  Verilator and GHDL: all linked; NVC: all preloaded). The cache keys hold each stand-in's configuration.
- `tests/test_xfft_multi.py`: two instances of one configuration and one of another in one simulation, each
  instance's handshakes equal to those of the same traffic on its configuration alone; CI runs it on NVC and GHDL.
  `XFFT_MULTI_CMODEL=1` runs it with the C model and random data (every output frame also checked against a
  floating-point FFT), `XFFT_MULTI_A` and `XFFT_MULTI_B` choose the two configurations (constants from the timing
  reference when model/xfft_timing.py has none).

### Changed

- A configuration whose module is not `xfft_core`: compile `<dir>/<module>_pkg.sv` (or `.vhdl`) and the wrapper
  copy `<dir>/<module>.sv` instead of `<dir>/xfft_model_pkg.sv` and `model/xfft_engine.sv`, and load
  `lib<module>_engine.so`; the VHDL wrapper copy is `<dir>/<module>.vhdl` (was `<dir>/xfft_engine.vhdl`).
- `model/xfft_engine.sv` no longer defines `XFFT_CORE_NAME` and `XFFT_ENGINE_NAME`; it only reads them, so they do
  not reach the files compiled after it.
- `module` must be a VHDL basic identifier; `xfft`, `xfft_model` and `xfft_engine`, and names that differ from
  them or from `xfft_core` only in case, are refused (their names would be the default's).
- A configuration whose module is not `xfft_core` takes the timing constants of the same configuration with
  module `xfft_core` when there are none under its own tag (the module's name does not change the timing).
- `tests/timing/tb_xfft_timing.sv`, `tests/xfft_pair.sv` and `examples/plain_xsim/tb_tone.sv` take the package's
  name from `` `XFFT_MODEL_PKG `` (default `xfft_model_pkg`), and `tb_xfft_timing.sv` the C name of
  `xfft_engine_wallclock` from `` `XFFT_ENGINE_WALLCLOCK `` (default `xfft_engine_wallclock`).
- The result and elaboration cache entries of 0.1.0 are not reused (the keys now hold the stand-in's
  configuration).

### Fixed

- The S3 unit tests no longer pick up the machine's own AWS profile (`~/.aws`, `AWS_CONFIG_FILE`): with a profile
  that names an endpoint, they sent their requests to that store and failed.

## [0.1.0] - 2026-11-14

First public version.

- The stand-in for AMD's LogiCORE FFT (xfft 9.x, Pipelined Streaming I/O): one C engine holds all of its timing;
  thin wrappers with the core's module name and ports run it on XSim and Verilator (SystemVerilog, DPI-C) and on
  NVC and GHDL (VHDL-2008, VHPIDIRECT). The data comes from AMD's bit-accurate C model of the generated core.
- Input timing near a full input FIFO: after input gaps the stand-in refuses input where the core does, also for
  dense and clustered gap patterns, at any transform length.
- Riviera-PRO: the stand-in through the SV wrapper (DPI-C), and the real core with AMD's simulation libraries
  compiled for Riviera (`XFFT_RIVIERA_LIBS`); the stand-in's traces on Riviera equal XSim's.
- The characterization (`python -m characterize`, CLI and Python API): generates the core with Vivado from a
  configuration or an existing `.xci`, measures its timing constants in XSim, and checks the stand-in built from
  them against the core. Refuses Versal parts and unsupported architectures.
- Result cache and elaboration cache, in a directory or an S3 bucket.
- Tests: cocotb tests of the core and the stand-in on every installed simulator, and unit tests that need no
  Vivado, among them the engine's input timing against reference vectors measured on the real core.
- Timing-only build of the engine (`scripts/build_engine.py --timing-only`): no Vivado and no C model, all-zero
  data, the same clocks. `tests/test_timing_reference.py` runs every characterization case on it and compares the
  clocks with the real core's, stored in `tests/reference/` (949 configurations, 136 KB: a binary format, xz,
  with its C++ unpacker in `tests/reference_codec/`); CI runs the checked configurations on NVC and GHDL.
