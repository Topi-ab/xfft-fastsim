# The config channel

Each frame takes the `s_axis_config` word offered before it: `FWD_INV` in bit 0 (1 forward) and `SCALE_SCH` above it, 2 bits per radix-4 stage (`XfftConfig.config_word()` builds it; `xfft_tb.Lane` offers each frame's word once the frame before it has started).

- `round_trip`: a tone through a forward transform with `SCALE_SCH` 2 per stage (about 1/N), then the result through an inverse transform with `SCALE_SCH` 1 per stage: the tone comes back, scaled by N / 2^(sum of both schedules), within a few LSB. The core's inverse is unnormalised (N times numpy's `ifft`);
- `overflow`: full-scale random data with `SCALE_SCH` 0 overflows: `OVFLO` is set in `m_axis_data_tuser` and in the frame's status word. The stand-in's data after an overflow is not the core's (only the flag is exact), so only the flag is checked.

```sh
FFT_MODEL=engine pytest examples/config_channel
FFT_MODEL=rtl pytest examples/config_channel
```
