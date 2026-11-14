# Plain XSim

`tb_tone.sv` instantiates the core (`xfft_core`, here the stand-in, the C engine's SV wrapper, under that name), sends one config word (forward, scaled by about 1/N) and one frame of a complex tone at a quarter of full scale, and reads the frame's output. It checks that the outputs come in natural order (`XK_INDEX`) with `tlast` on the last, that the largest magnitude is at the tone's bin, and that the first output comes `XFFT_LATENCY_FIRST_OUT` clocks after the first input. It takes all widths and constants from the generated package `xfft_model_pkg`, so it follows the configuration.

```sh
examples/plain_xsim/run.sh
```

prints

```
tb_tone: 256 points, 16-bit data: peak at bin 35 (expected 35), magnitude 8191.0
tb_tone: first input -> first output 627 clocks (XFFT_LATENCY_FIRST_OUT = 627)
PASS
```

`run.sh` is the recipe for a testbench of your own, in four commands:

1. `python scripts/build_engine.py <dpi dir>`: the C model (generating the core with Vivado the first time), the C engine's library and the SV package;
2. `xvlog -sv -work xil_defaultlib -d XFFT_CORE_NAME=xfft_core <dpi dir>/xfft_model_pkg.sv model/xfft_engine.sv tb_tone.sv`;
3. `xelab -L xil_defaultlib -sv_root <dpi dir> -sv_lib libxfft_engine -snapshot tb_tone xil_defaultlib.tb_tone`;
4. `xsim tb_tone -R`.

The library's directory is not the elaboration directory, so the snapshot finds the library from anywhere. For a configuration whose module is not `xfft_core`, `run.sh` takes that module's names instead: the package `<module>_pkg` (`-d XFFT_MODEL_PKG=<module>_pkg`), the wrapper copy `<dpi dir>/<module>.sv` and the library `lib<module>_engine`. Everything goes to `build/examples/plain_xsim/`. `XFFT_CONFIG=... examples/plain_xsim/run.sh` runs it on another configuration with timing constants.
