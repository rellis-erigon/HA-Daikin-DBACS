"""Install the Home Assistant stand-ins before any test module imports.

A no-op in CI, which installs the real package. See _ha_stub for why.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _ha_stub  # noqa: E402

_ha_stub.install()


import pytest  # noqa: E402

from custom_components.daikin_dbacs import mapping  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_site_data(tmp_path, monkeypatch):
    """Keep every test away from the live site mapping files.

    `path_for` builds a path under `SITE_DIR`, which on the machine this is
    developed on holds the real building's 96 and 128 unit mappings. A test
    that set up a config entry would quietly read them, so its result
    depended on whether it ran on that machine or in CI. Pointing SITE_DIR
    at an empty directory makes every test behave the same way everywhere:
    there is no mapping unless the test writes one.
    """
    monkeypatch.setattr(mapping, "SITE_DIR", str(tmp_path / "site"))
