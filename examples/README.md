# Examples

Three small examples on the 256-point core (`XFFT_CONFIG=nfft=8`, whose timing constants ship in `timing/`). Each runs with one command from the repository root, with Vivado's `settings64.sh` sourced and the venv active. The first run of the configuration generates the core with Vivado (a minute or two); after that each example takes seconds on the stand-in.

| Example | What it shows | Command |
|---|---|---|
| [plain_xsim](plain_xsim/) | the stand-in in a plain SystemVerilog testbench, no cocotb: the three tool steps a testbench of your own needs | `examples/plain_xsim/run.sh` |
| [cocotb_backpressure](cocotb_backpressure/) | input gaps and output stalls, data against numpy, latencies against the timing constants; the same test on either model; the result cache | `FFT_MODEL=engine pytest examples/cocotb_backpressure` |
| [config_channel](config_channel/) | the per-frame config word: forward then inverse (a round trip), and an overflow with `SCALE_SCH` 0 | `FFT_MODEL=engine pytest examples/config_channel` |

`FFT_MODEL=engine` runs them on the stand-in, the C engine: on XSim, and on NVC, GHDL and Verilator where they are installed (`examples/plain_xsim/run.sh` on its SV wrapper in XSim). `FFT_MODEL=rtl` runs the cocotb examples on the real core (its encrypted simulation model) instead: the same test, slower, for sign-off. `XFFT_CONFIG` picks another configuration, which needs its timing constants (docs/CHARACTERIZATION.md); the shipped ones are listed in the README.

`tests/test_examples.py` runs all three on the stand-in as a smoke test: `pytest tests/test_examples.py`.
