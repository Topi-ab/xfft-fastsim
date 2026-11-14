# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

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
