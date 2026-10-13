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
        path = write(tmp_path, {"1 · 2-14": {"name": "Room 412"}})
        loaded = mapping.load(path)
        assert loaded[30].name == "Room 412"  # port 1, line 2, group 14

    @pytest.mark.parametrize(
        "key", ["1 · 2-14", "1 2-14", "1-2-14", "1/2/14", "P1 L2 G14"]
    )
    def test_the_address_however_it_was_typed(self, tmp_path, key):
        # The file is maintained by hand. Rejecting it over a middle dot
        # would be absurd.
        path = write(tmp_path, {key: {"name": "Room 412"}})
        assert mapping.load(path)[30].name == "Room 412"

    def test_an_explicit_ac_number_wins(self, tmp_path):
        path = write(tmp_path, {"whatever": {"ac_number": 30, "name": "X"}})
        assert mapping.load(path)[30].name == "X"

    def test_every_field_is_read(self, tmp_path):
        path = write(tmp_path, {"2 · 1-00": {
            "name": "Room 457", "area": "L4 Accommodation", "tag": "ACU L4-09",
            "model": "FXDQ40TV1B", "system": "H-23", "outdoor": "CU-H-23",
        }})
        entry = mapping.load(path)[64]
        assert entry.tag == "ACU L4-09"
        assert entry.outdoor == "CU-H-23"
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
            "1 · 1-00": {"name": "Room 401"},
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
            0: mapping.UnitMapping(0, name="Room 401 Bed", area="L4 Accommodation"),
            1: mapping.UnitMapping(1, name="Room 403", area="L4 Accommodation"),
        }

    def test_an_unnamed_unplaced_device_gets_both(self):
        result = mapping.plan(self._mapping(), {0: _Device()})
        fields = {c.field for c in result.changes}
        assert fields == {"name", "area"}
        assert result.areas_to_create == ["L4 Accommodation"]

    def test_an_existing_area_is_not_created_again(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()},
            existing_areas={"L4 Accommodation": "l4_acc"},
        )
        assert result.areas_to_create == []
        assert [c.field for c in result.changes] == ["name", "area"]

    def test_area_matching_ignores_case_and_padding(self):
        result = mapping.plan(
            self._mapping(), {0: _Device()},
            existing_areas={"  l4 ACCOMMODATION ": "l4_acc"},
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
            self._mapping(), {0: _Device(name_by_user="Room 401 Bed")},
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
