---
name: Stand-in differs from the real core
about: A configuration or a traffic pattern where the stand-in's output or timing is not what the core does
title: "Mismatch: <configuration> <what differs>"
labels: mismatch
---

<!--
Thank you. A mismatch report is most useful when it can be rerun unchanged: the configuration, the traffic that
breaks it, and both models. The steps below produce all of it. Delete the hints you do not need.
-->

## What differs

<!-- One or two sentences: which channel, which signal, real core vs stand-in. Example: "m_axis_data_tvalid of
frame 2 rises 4 clocks earlier in the stand-in after a 1-clock input gap at sample 96". -->

## Environment

| | |
|---|---|
| Repository commit | <!-- git rev-parse --short HEAD --> |
| Vivado version | <!-- vivado -version, first line --> |
| xfft core version | <!-- get_property IPDEF [get_ips xfft_core], e.g. xilinx.com:ip:xfft:9.1 --> |
| OS, Python | <!-- e.g. Ubuntu 24.04, Python 3.12 --> |
| Timing constants | <!-- where the stand-in's constants came from: TABLE, timing/<tag>.json, or a characterization you ran --> |

## Configuration

The `XFFT_CONFIG` value of the failing run. To print it, run this in the same shell, with the same environment as
the failing run:

```sh
python -c "import sys; sys.path.insert(0, 'model'); import xfft_config as c; print('XFFT_CONFIG=' + c.current().spec)"
```

Paste the printed line here (`XFFT_CONFIG=` with nothing after it is the default configuration).

If you made the core outside this repository (your own IP settings), run
`python -m characterize --xci <your .xci> --dry-run` and paste its output: it shows the configuration it maps to
and every option in which your IP differs from the core the repository generates.

## Reproducer

The traffic that breaks it, as `xfft_tb.Frame` objects (the format of `tests/xfft_tb.py` and the characterization
cases), for example:

```python
from xfft_tb import Frame
frames = [
    Frame(data, True, schedule),                               # data: N complex integers; schedule: SCALE_SCH per stage
    Frame(data, False, schedule, gaps={96: 1, 160: 1}),        # gaps[k]: clocks of s_axis_data_tvalid low before sample k
    Frame(data, True, schedule, stalls={100: 40}),             # stalls[k]: clocks of m_axis_data_tready low before output k
]
```

Best: a cocotb test like `examples/cocotb_backpressure/test_backpressure.py` with these frames that passes with
`FFT_MODEL=rtl` and fails with `FFT_MODEL=engine` (or the reverse). Attach the test and both runs' `sim.log`
(in `build/sim/<build name>/<sim>[_<model>]/`, for example `build/sim/<tag>/<test>/vivado/` for the real core and
`build/sim/<tag>/<test>/vivado_xfft_engine/` for the stand-in); with `WAVES=1` also the waveform databases of both
runs. If the stand-in differs between simulators (XSim, NVC, GHDL, Verilator), say on which.

## Checks done

- [ ] The run used the stand-in's constants for exactly this configuration (its tag), not those of another.
- [ ] `pytest tests/test_xfft_compare.py` with the same `XFFT_CONFIG`: passes / fails (say which).
- [ ] `python -m characterize` with the same configuration: exit code 0 / 1 / 3 / not run.
