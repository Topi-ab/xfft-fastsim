#!/usr/bin/env bash
# Plain XSim example: builds the stand-in (the C engine and its SV wrapper) for the configuration, compiles
# tb_tone.sv with it and runs it.
#
#   examples/plain_xsim/run.sh                      (from anywhere; Vivado's settings64.sh sourced)
#
# XFFT_CONFIG picks the configuration (default here: nfft=8, the 256-point core). The three tool steps are the
# ones a testbench of your own needs: build_engine.py, xvlog with the package and the stand-in, xelab with the
# DPI-C library. Everything goes to build/examples/plain_xsim/. Exit code 0 when the bench prints PASS.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
python=${PYTHON:-python3}
export XFFT_CONFIG=${XFFT_CONFIG-nfft=8}

work="$root/build/examples/plain_xsim/xfft_engine"
dpi="$work/dpi"            # the C engine's library and the package; not the elaboration directory
sim="$work/sim"
mkdir -p "$sim"
module=$("$python" -c "import sys; sys.path.insert(0, '$root/model'); import xfft_config
print(xfft_config.current().module)")

# 1. The stand-in's C side: generates the core with Vivado the first time (its C model), compiles the C engine
"$python" "$root/scripts/build_engine.py" "$dpi"

cd "$sim"
# 2. Compile: the package, the stand-in under the core's name, the testbench
xvlog -sv -work xil_defaultlib -d XFFT_CORE_NAME="$module" \
    "$dpi/xfft_model_pkg.sv" "$root/model/xfft_engine.sv" "$here/tb_tone.sv" > xvlog.log
# 3. Elaborate with the DPI-C library (-sv_root: its directory, so the snapshot finds it from anywhere)
xelab -L xil_defaultlib -sv_root "$dpi" -sv_lib libxfft_engine -snapshot tb_tone xil_defaultlib.tb_tone > xelab.log
# 4. Run
xsim tb_tone -R -log xsim.log > /dev/null
grep "^tb_tone:\|^PASS\|^FAIL" xsim.log
grep -q "^PASS" xsim.log
