"""One mapping file per gateway.

A DIII-NET address is only unique within one gateway: both gateways on this
site number their units from the same formula, so address 1 · 1-00 exists on
each and means a different room on each. The mapping file therefore has to
be chosen per gateway, and the first build chose it once globally — which
would have named the second gateway's 147 units after the first gateway's
rooms, plausibly and silently.
"""

from __future__ import annotations

import json

import pytest

from custom_components.daikin_dbacs import mapping
from custom_components.daikin_dbacs.mapping import (
    SITE_DIR as REAL_SITE_DIR,
    MappingError,
    load,
    path_for,
)


class TestPathForGateway:
    def test_each_instance_gets_its_own_file(self):
        assert path_for(11) != path_for(12)

    def test_the_path_is_built_from_the_instance(self):
        # mapping.SITE_DIR, not the value imported at module scope: the
        # suite redirects it per test so nothing reads the real building's
        # mapping files. See conftest.
        assert path_for(12) == f"{mapping.SITE_DIR}/gateway-12-units.json"

    def test_the_established_file_name_is_unchanged(self):
        """Gateway 11's file is already in use and must not be renamed."""
        assert path_for(11) == f"{mapping.SITE_DIR}/gateway-11-units.json"

    def test_the_real_location_is_outside_the_configuration_directory(self):
        """Site data lives outside the configuration directory, in a folder
        of its own that no other project owns.

        This one is about the shipped default rather than the redirected
        one, so it reads the constant as imported.
        """
        assert REAL_SITE_DIR == "/share/daikin_dbacs"

    @pytest.mark.parametrize("instance", [None, "", "eleven", -1])
    def test_an_instance_that_is_not_a_number_is_refused(self, instance):
        """Better to say there is no mapping than to read the wrong one."""
        with pytest.raises(MappingError):
            path_for(instance)

    def test_a_numeric_string_is_accepted(self):
        """Config entry data arrives from JSON and may be a string."""
        assert path_for("12") == path_for(12)


class TestLoadingPerGateway:
    def test_two_gateways_load_different_units(self, tmp_path):
        for instance, room in ((11, "Room A1"), (12, "Room B1")):
            payload = {"1 · 1-00": {"ac_number": 0, "name": room}}
            (tmp_path / f"gateway-{instance}-units.json").write_text(
                json.dumps(payload)
            )

        eleven = load(str(tmp_path / "gateway-11-units.json"))
        twelve = load(str(tmp_path / "gateway-12-units.json"))

        # Same address, different room. This is the whole point.
        assert eleven[0].name == "Room A1"
        assert twelve[0].name == "Room B1"

    def test_a_missing_file_is_reported_not_guessed(self, tmp_path):
        with pytest.raises(MappingError):
            load(str(tmp_path / "gateway-99-units.json"))
