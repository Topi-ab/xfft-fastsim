# Backpressure with cocotb

`test_backpressure.py` drives the core as the toplevel with `tests/xfft_tb.py`'s `Lane`, which records every handshake with its clock:

- `stalls_and_gaps`: one frame with input gaps of 1 and 4 clocks and output stalls of 3 and 20 clocks. The 20-clock stall is longer than the core's output queue, so its pipeline pauses. The output must match numpy's FFT within a few LSB, and the latencies and the status word's clock must be what the timing constants and the rules of `model/xfft_timing.py` predict;
- `random_traffic`: four frames back to back, forward and inverse, with random gaps and stalls; every frame's data must be right.

```sh
FFT_MODEL=engine pytest examples/cocotb_backpressure     # the stand-in (XSim, NVC, GHDL, Verilator, where installed)
FFT_MODEL=engine pytest examples/cocotb_backpressure     # again: from the cache in seconds (docs/CACHE.md)
FFT_MODEL=rtl pytest examples/cocotb_backpressure        # the same test on the real core
```

A run served from the cache ends with "1 passed, 1 warning": the warning (`CachedResult: ... passed 2 tests from the cache ...; not run`) says so. The `sim_cache:` log lines show with `pytest -s`.

The pytest function is all a test of your own needs: `sim_runner.run()` with `hdl_sources.xfft_core(cfg, model)` as the source, `FFT_MODEL` picking the model.
