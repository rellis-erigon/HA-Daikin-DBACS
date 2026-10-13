"""Install the Home Assistant stand-ins before any test module imports.

A no-op in CI, which installs the real package. See _ha_stub for why.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _ha_stub  # noqa: E402

_ha_stub.install()
