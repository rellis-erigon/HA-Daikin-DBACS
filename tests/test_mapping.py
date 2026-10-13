"""Applying a site's own names and areas.

A gateway knows a unit by its DIII address and nothing else, so everything
legible comes from a file somebody maintains by hand. These tests are about
being forgiving of that file and careful with the building: a choice already
made always wins, and the plan is computed before anything is touched.
"""

from __future__ import annotations

import json

import pytest

from custom_components.daikin_dbacs import mapping


class _Device:
    def __init__(self, name_by_user=None, area_id=None) -> None:
        self.name_by_user = name_by_user
        self.area_id = area_id


def write(tmp_path, data) -> str:
    path = tmp_path / "units.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestReadingTheFile:
    def test_the_address_as_the_integration_writes_it(self, tmp_path):
        path = write(tmp_path, {"1 · 2-14": {"name": "Room A1"}})
        loaded = mapping.load(path)
        assert loaded[30].name == "Room A1"  # port 1, line 2, group 14

    @pytest.mark.parametrize(
        "key", ["1 · 2-14", "1 2-14", "1-2-14", "1/2/14", "P1 L2 G14"]
    )
    def test_the_address_however_it_was_typed(self, tmp_path, key):
        # The file is maintained by hand. Rejecting it over a middle dot
        # would be absurd.
        path = write(tmp_path, {key: {"name": "Room A1"}})
        assert mapping.load(path)[30].name == "Room A1"

    def test_an_explicit_ac_number_wins(self, tmp_path):
        path = write(tmp_path, {"whatever": {"ac_number": 30, "name": "X"}})
        assert mapping.load(path)[30].name == "X"

    def test_every_field_is_read(self, tmp_path):
        path = write(tmp_path, {"2 · 1-00": {
            "name": "Room A2", "area": "Block A", "tag": "ACU A-09",
            "model": "FXDQ40TV1B", "system": "S-01", "outdoor": "CU-S-01",
        }})
        entry = mapping.load(path)[64]
        assert entry.tag == "ACU A-09"
        assert entry.outdoor == "CU-S-01"
        assert entry.label == "2 · 1-00"

    def test_a_missing_file_says_what_it_is_for(self, tmp_path):
        with pytest.raises(mapping.MappingError) as caught:
            mapping.load(str(tmp_path / "nope.json"))
        message = str(caught.value)
        assert "not shipped with the integration" in message

    def test_broken_json_says_so(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(mapping.MappingError) as caught:
            mapping.load(str(path))
        assert "not valid JSON" in str(caught.value)

    def test_a_list_is_refused(self, tmp_path):
        path = write(tmp_path, [{"name": "x"}])
        with pytest.raises(mapping.MappingError):
            mapping.load(path)

    def test_an_unreadable_address_is_skipped_not_fatal(self, tmp_path):
        path = write(tmp_path, {
            "nonsense": {"name": "ignored"},
            "1 · 1-00": {"name": "Room A1"},
        })
        loaded = mapping.load(path)
        assert list(loaded) == [0]

    def test_an_address_outside_the_gateway_is_skipped(self, tmp_path):
        path = write(tmp_path, {"9 · 1-00": {"name": "x"}})
        with pytest.raises(mapping.MappingError):
            mapping.load(path)

    def test_a_file_with_nothing_usable_is_an_error(self, tmp_path):
        path = write(tmp_path, {"nonsense": {"name": "x"}})
        with pytest.raises(mapping.MappingError) as caught:
            mapping.load(path)
        assert "no usable entries" in str(caught.value)


class TestThePlan:
    def _mapping(self):
        return {
            0: mapping.UnitMapping(0, name="Room A1 Bed", area="Block A"),
            1: mapping.UnitMapping(1, name="Room A2", area="Block A"),
        }

    def test_an_unnamed_unplaced_device_gets_both(self):
        result = mapping.plan(self._mapping(), {0: _Device()})
        fields = {c.field for c in result.changes}
        assert fields == {"name", "area"}
        assert result.areas_to_create == ["Block A"]

    def test_an_existing_area_is_not_created_again(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()},
            existing_areas={"Block A": "block_a"},
        )
        assert result.areas_to_create == []
        assert [c.field for c in result.changes] == ["name", "area"]

    def test_area_matching_ignores_case_and_padding(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()},
            existing_areas={"  block A ": "block_a"},
        )
        assert result.areas_to_create == []

    def test_a_name_somebody_chose_is_left_alone(self):
        # The person who renamed it knew something the file does not.
        result = mapping.plan(
            self._mapping(), {0: _Device(name_by_user="The Boardroom")}
        )
        assert [c.field for c in result.changes] == ["area"]
        assert any("already" in k for k in result.kept)

    def test_an_area_somebody_chose_is_left_alone(self):
        result = mapping.plan(
            self._mapping(), {0: _Device(area_id="somewhere")}
        )
        assert [c.field for c in result.changes] == ["name"]

    def test_overwrite_takes_both_anyway(self):
        result = mapping.plan(
            self._mapping(),
            {0: _Device(name_by_user="The Boardroom", area_id="somewhere")},
            overwrite=True,
        )
        assert {c.field for c in result.changes} == {"name", "area"}

    def test_nothing_changes_when_it_already_matches(self):
        result = mapping.plan(
            self._mapping(), {0: _Device(name_by_user="Room A1 Bed")},
            set_areas=False, overwrite=True,
        )
        assert result.changes == []

    def test_names_only(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()}, set_areas=False
        )
        assert [c.field for c in result.changes] == ["name"]

    def test_areas_only(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()}, set_names=False
        )
        assert [c.field for c in result.changes] == ["area"]

    def test_a_live_group_the_file_omits_is_reported(self):
        result = mapping.plan(self._mapping(), {0: _Device(), 5: _Device()})
        assert result.unmapped == ["1 · 1-05"]

    def test_a_file_entry_with_no_live_group_is_reported(self):
        result = mapping.plan(self._mapping(), {0: _Device()})
        assert result.unknown == ["1 · 1-01"]

    def test_the_summary_is_reportable(self):
        result = mapping.plan(self._mapping(), {0: _Device()})
        summary = result.summary()
        assert summary["change_count"] == 2
        assert summary["applied_count"] == 0
        assert summary["changes"][0]["address"] == "1 · 1-00"
        assert summary["changes"][0]["applied"] is False

    def test_an_entry_with_neither_name_nor_area_does_nothing(self):
        result = mapping.plan({0: mapping.UnitMapping(0)}, {0: _Device()})
        assert result.changes == []


class TestTheGeneratedCard:
    """What a bridge hands the dashboard strategy.

    The chain from here to something visible is long — integration, service,
    strategy, card, faceplate — and a mistake anywhere renders a card that
    is simply dark. So the shape is asserted here rather than discovered on
    a dashboard.
    """

    def test_a_card_binds_from_the_device_alone(self):
        from custom_components.daikin_dbacs import cards

        card = cards.card_for(device_name="Room A1 ACU A-01")
        assert card["type"] == "custom:hvac-controller-card"
        assert card["device"] == "Room A1 ACU A-01"
        # No entities block: the shared role vocabulary binds a unit from
        # the device, and sixteen written-out ids would be wrong the moment
        # somebody enabled a seventeenth point.
        assert "entities" not in card

    def test_the_full_status_face_is_the_default(self):
        from custom_components.daikin_dbacs import cards

        assert cards.card_for(device_name="x")["faceplate"] == "indoor-unit"

    def test_a_site_can_choose_the_face(self):
        from custom_components.daikin_dbacs import cards

        card = cards.card_for(device_name="x", faceplate="fcu-unit")
        assert card["faceplate"] == "fcu-unit"

    def test_a_thermostat_is_offered_as_well_as_the_device(self):
        # Both, deliberately: the card prefers a per-role binding and falls
        # back to the climate entity, so the lamps come from the device's
        # entities while control comes from the thermostat. Either alone
        # loses something.
        from custom_components.daikin_dbacs import cards

        card = cards.card_for(device_name="x", climate_entity="climate.x")
        assert card["climate"] == "climate.x"
        assert card["device"] == "x"

    def test_no_thermostat_means_no_climate_key(self):
        from custom_components.daikin_dbacs import cards

        assert "climate" not in cards.card_for(device_name="x")

    def test_the_strategy_entry_carries_the_area(self):
        from custom_components.daikin_dbacs import cards

        entry = cards.generated(device_name="x", area_id="l5_accommodation")
        assert entry["device"] == "x"
        assert entry["area_id"] == "l5_accommodation"
        assert entry["card"]["device"] == "x"

    def test_an_unplaced_unit_still_gets_an_entry(self):
        # A unit nobody has placed yet is still a unit somebody wants to
        # see, so it appears under "no area" rather than not at all.
        from custom_components.daikin_dbacs import cards

        entry = cards.generated(device_name="Hall ACU A-C1")
        assert "area_id" not in entry
        assert entry["device"] == "Hall ACU A-C1"

    def test_the_title_is_the_device_name(self):
        from custom_components.daikin_dbacs import cards

        entry = cards.generated(device_name="Room A1 ACU A-01")
        assert entry["card"]["title"] == "Room A1 ACU A-01"

    def test_a_faceplate_from_the_mapping_file_is_read(self, tmp_path):
        path = write(tmp_path, {"1 · 1-00": {
            "name": "Room A1", "faceplate": "fcu-unit"}})
        assert mapping.load(path)[0].faceplate == "fcu-unit"
