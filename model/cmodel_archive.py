"""The C model of a generated xfft core: the IP's cmodel output product (generate_target cmodel, written by the
builder Tcl of model/xfft_config.py), so it always matches the core's version. No path or version of the Vivado
installation is written in the code.

The archive's header, library and API symbols carry the IP version in their names (xfft_v9_1_bitacc_cmodel.h,
libIp_xfft_v9_1_bitacc_cmodel.so, xilinx_ip_xfft_v9_1_*): they are read from its contents. Its own dependencies
(gmp, mpfr, the floating-point C model) are linked by directory, so their versions do not matter.
Standard library only.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


class CModelError(Exception):
    """The generated IP has no usable C model."""


def find(ip_dir: Path) -> Path:
    """The Linux C-model archive in a generated IP's directory (<ip dir>/cmodel/*bitacc_cmodel_lin64.zip)."""
    archives = sorted((Path(ip_dir) / "cmodel").glob("*bitacc_cmodel_lin64.zip"))
    if len(archives) != 1:
        raise CModelError(f"expected one *bitacc_cmodel_lin64.zip in {Path(ip_dir) / 'cmodel'}, found "
                          f"{len(archives)}: was the IP generated with its cmodel target?")
    return archives[0]


@dataclass(frozen=True)
class CModel:
    """Names inside a C-model archive: the header, the library (for -l) and the prefix of the API symbols."""
    header: str
    library: str
    api_prefix: str
    version: str


def names(archive: Path) -> CModel:
    with zipfile.ZipFile(archive) as z:
        files = [Path(n).name for n in z.namelist()]
    headers = [m for n in files for m in [re.fullmatch(r"xfft_v(\d+)_(\d+)_bitacc_cmodel\.h", n)] if m]
    libs = [n for n in files if re.fullmatch(r"libIp_xfft_v\d+_\d+_bitacc_cmodel\.so", n)]
    if len(headers) != 1 or len(libs) != 1:
        raise CModelError(f"{archive}: expected one xfft_v*_bitacc_cmodel.h and one libIp_xfft_v*_bitacc_cmodel.so, "
                          f"found {[m[0] for m in headers]} and {libs}")
    h = headers[0]
    if int(h[1]) != 9:
        raise CModelError(f"{archive}: xfft {h[1]}.{h[2]}; model/xfft_dpi.c is written for the xfft 9.x C model")
    return CModel(header=h[0], library=libs[0][3:-3], api_prefix=f"xilinx_ip_xfft_v{h[1]}_{h[2]}_",
                  version=f"{h[1]}.{h[2]}")
