"""The device families the characterization supports: 7-series, Zynq UltraScale+ and Virtex UltraScale+, not
Versal. On Versal the real xfft core's XSim simulation outputs all-zero data for any input (its handshake timing
is right), so there is no real-core reference to characterize against; the characterization refuses a Versal
part before it generates anything. The stand-in itself is not restricted.

Vivado's own ARCHITECTURE / FAMILY of the part decides when it is known (the .xci reader asks Vivado for them);
without Vivado the part name does: Versal parts start with xcvc, xcve, xcvm, xcvp, xcvh, xcvr (and the xqv / xav
variants), Versal Gen 2 parts with xc2v. Virtex UltraScale+ (xcvu), Virtex-7 (xc7v), Kintex, Artix and Zynq do
not match.
"""

from __future__ import annotations

import re

VERSAL_NAME = re.compile(r"x[cqa](v[cemphr]|2v)", re.IGNORECASE)


def is_versal(part: str, architecture: str = "", family: str = "") -> bool:
    """Whether the part is a Versal device: by Vivado's ARCHITECTURE or FAMILY of the part when either is given,
    else by its name."""
    if architecture or family:
        return any(p.lower().startswith("versal") for p in (architecture, family))
    return VERSAL_NAME.match(part.strip()) is not None


def versal_refusal(part: str) -> str:
    """The error of a Versal part."""
    return (f"part {part}: Versal is not supported by the characterization: on Versal the real xfft core's XSim "
            f"simulation outputs all-zero data for any input, so it gives no reference to characterize against "
            f"(supported: 7-series, Zynq UltraScale+, Virtex UltraScale+)")
