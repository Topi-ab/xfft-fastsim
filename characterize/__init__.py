"""Characterization of the xfft core's timing for the stand-in: python -m characterize, or from Python:

    from characterize import characterize
    result = characterize("nfft=8", jobs=4)

See characterize/api.py and docs/CHARACTERIZATION.md. The measurement itself is the bench in characterize/bench/.
"""

import sys
from pathlib import Path

__version__ = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()

_REPO = Path(__file__).resolve().parents[1]
for _path in (_REPO / "characterize" / "bench", _REPO / "tests", _REPO / "scripts", _REPO / "model"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from .api import (EXIT_COMPARE, EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, CharacterizeError, Result,  # noqa: E402
                 characterize, resolve_config)
from .xci import XciError, XciReport, config_from_xci, read_config  # noqa: E402

__all__ = ["characterize", "resolve_config", "config_from_xci", "read_config", "Result", "XciReport",
           "CharacterizeError", "XciError", "EXIT_OK", "EXIT_PROBLEMS", "EXIT_USAGE", "EXIT_COMPARE"]
