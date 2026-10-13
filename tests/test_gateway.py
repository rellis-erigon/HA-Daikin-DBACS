"""The BACnet client, driven against a synthetic gateway.

The real :class:`DbacsGateway` is under test here — its batching, its error
vocabulary and what it does when part of a read fails. Only the bacpypes3
Application underneath it is faked, so the behaviour being checked is the
behaviour that will run.

Tests are driven with `asyncio.run` rather than an async plugin, so the
suite needs nothing but pytest.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.daikin_dbacs import gateway as gw
from custom_components.daikin_dbacs.objects import (
    MEMBERS_BY_KEY,
    PointRef,
    instance_of,
    points_for,
)

import fake_gateway as fake


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state():
    return fake.FakeGatewayState()


@pytest.fixture
def gateway(state):
    client = gw.DbacsGateway("192.0.2.10")
    fake.attach(client, state)
    return client


class TestIdentity:
    def test_reads_the_device_object(self, gateway, state):
        info = run(gateway.read_info())
        assert info.device_instance == state.device_instance
        assert info.model_name == "DMS502B71"
        assert info.max_apdu_length == 1024
        assert info.database_revision == 7

    def test_learns_the_device_instance_by_unicast_who_is(self, gateway, state):
        gateway.device_instance = None
        info = run(gateway.read_info())
        assert info.device_instance == state.device_instance

    def test_falls_back_to_the_wildcard_instance_when_who_is_is_silent(
        self, gateway, state
    ):
        # I-Am is normally broadcast, so a reachable gateway routinely
        # appears not to answer a unicast Who-Is. Reading the device object
        # at instance 4194303 is one plain unicast request and works where a
        # broadcast cannot.
        state.answers_who_is = False
        gateway.device_instance = None
        info = run(gateway.read_info())
        assert info.device_instance == state.device_instance

    def test_prefers_i_am_when_there_is_one(self, gateway, state):
        state.answers_wildcard = False
        gateway.device_instance = None
        info = run(gateway.read_info())
        assert info.device_instance == state.device_instance

    def test_says_what_to_do_when_neither_way_works(self, gateway, state):
        state.answers_who_is = False
        state.answers_wildcard = False
        gateway.device_instance = None
        with pytest.raises(gw.DbacsUnreachable) as caught:
            run(gateway.read_info())
        # The message has to carry the remedy: the instance number comes
        # from whoever commissioned the box.
        message = str(caught.value)
        assert "instance" in message
        assert "Trial Operation tool" in message

    def test_an_unreachable_gateway_is_reported_as_such(self, gateway, state):
        gateway.device_instance = None
        state.offline = True
        with pytest.raises(gw.DbacsUnreachable):
            run(gateway.read_info())

    def test_a_wildcard_answer_of_the_wildcard_is_not_an_answer(
        self, gateway, state, monkeypatch
    ):
        state.answers_who_is = False
        state.answers_wildcard = False
        gateway.device_instance = None

        async def answers_with_the_wildcard(*_args, **_kwargs):
            return ("device", gw.WILDCARD_INSTANCE)

        monkeypatch.setattr(
            gateway._app, "read_property", answers_with_the_wildcard
        )
        with pytest.raises(gw.DbacsUnreachable):
            run(gateway.read_info())

    def test_restart_token_changes_when_the_gateway_restarts(
        self, gateway, state
    ):
        before = run(gateway.read_info()).restart_token
        state.database_revision += 1
        after = run(gateway.read_info()).restart_token
        assert before != after


class TestReadingPoints:
    def test_reads_what_was_asked_for(self, gateway):
        points = points_for(3, ["room_temp", "setpoint", "on_off_status"])
        readings = run(gateway.read_points(points))
        assert set(readings) == {p.key for p in points}
        assert all(reading.ok for reading in readings.values())
        assert readings["3_room_temp"].value == pytest.approx(21.5)

    def test_batches_by_indoor_unit(self, gateway, state):
        # One unit's points must arrive together, or a card can show half a
        # stale unit.
        points = points_for(1) + points_for(2)
        run(gateway.read_points(points))
        for call in state.rpm_calls:
            units = {
                instance // 256 for instance in fake.instances_in(call)
            }
            assert len(units) == 1

    def test_keeps_each_chunk_within_the_apdu_budget(self, gateway, state):
        run(gateway.read_points(points_for(1)))
        assert state.rpm_calls
        for call in state.rpm_calls:
            assert len(call) <= gw.RPM_CHUNK

    def test_an_absent_group_is_reported_as_unknown_object(self, gateway):
        # 200 is outside the synthetic gateway's populated range.
        readings = run(gateway.read_points(points_for(200, ["room_temp"])))
        reading = readings["200_room_temp"]
        assert not reading.ok
        assert "unknown-object" in reading.error

    def test_one_broken_point_does_not_cost_the_others(self, gateway, state):
        state.broken.add(instance_of(4, MEMBERS_BY_KEY["setpoint"].number))
        readings = run(gateway.read_points(points_for(4)))
        assert not readings["4_setpoint"].ok
        assert readings["4_room_temp"].ok

    def test_a_point_the_gateway_omits_is_not_left_as_a_hole(
        self, gateway, monkeypatch
    ):
        async def answer_with_nothing(_self, _parameter_list):
            return []

        monkeypatch.setattr(gw.DbacsGateway, "_rpm", answer_with_nothing)
        readings = run(gateway.read_points(points_for(1, ["room_temp"])))
        assert readings["1_room_temp"].error == "no result returned"

    def test_an_unreachable_gateway_marks_the_points_not_the_sweep(
        self, gateway, state
    ):
        state.offline = True
        readings = run(gateway.read_points(points_for(1, ["room_temp"])))
        # Every point errors, but the call still returns: one dead gateway
        # must not take the whole coordinator down mid-sweep.
        assert all(not reading.ok for reading in readings.values())

    def test_a_gateway_still_scanning_stops_the_sweep(self, gateway, state):
        state.scanning_for = 99
        with pytest.raises(gw.DbacsNotReady):
            run(gateway.read_points(points_for(1, ["room_temp"])))


class TestProbingGroups:
    def test_finds_exactly_the_populated_groups(self, gateway, state):
        state.populated = {0, 1, 2, 64, 255}
        found = run(gateway.probe_groups(range(256)))
        assert {ac for ac, present in found.items() if present} == state.populated

    def test_asks_about_every_slot(self, gateway):
        found = run(gateway.probe_groups(range(256)))
        assert len(found) == 256

    def test_uses_one_cheap_point_per_group(self, gateway, state):
        run(gateway.probe_groups(range(16)))
        members = {
            instance % 256
            for call in state.rpm_calls
            for instance in fake.instances_in(call)
        }
        assert members == {MEMBERS_BY_KEY["on_off_status"].number}


class TestWaitingOutADiiiScan:
    def test_retries_until_the_gateway_is_ready(self, gateway, state, monkeypatch):
        monkeypatch.setattr(gw.asyncio, "sleep", _no_sleep)
        state.scanning_for = 3
        found = run(
            gateway.with_retry_when_not_ready(
                lambda: gateway.probe_groups(range(4))
            )
        )
        assert any(found.values())

    def test_gives_up_eventually_rather_than_hanging(
        self, gateway, state, monkeypatch
    ):
        monkeypatch.setattr(gw.asyncio, "sleep", _no_sleep)
        state.scanning_for = 10_000
        with pytest.raises(gw.DbacsNotReady):
            run(
                gateway.with_retry_when_not_ready(
                    lambda: gateway.probe_groups(range(4))
                )
            )


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("error_class", "error_code", "expected"),
        [
            ("device", "configuration-in-progress", gw.DbacsNotReady),
            ("device", "configurationInProgress", gw.DbacsNotReady),
            ("object", "unknown-object", gw.DbacsUnknownObject),
            ("property", "unknown-property", gw.DbacsUnknownObject),
            ("services", "cov-subscription-failed", gw.DbacsProtocolError),
            ("resources", "no-space-to-write-property", gw.DbacsProtocolError),
        ],
    )
    def test_maps_the_errors_daikin_documents(
        self, error_class, error_code, expected
    ):
        failure = fake.FakeError(error_class, error_code)
        assert isinstance(gw._map_error(failure), expected)

    def test_a_reject_without_an_error_code_is_still_reported(self):
        class Reject:
            apduAbortRejectReason = "segmentation-not-supported"  # noqa: N815

        mapped = gw._map_error(Reject())
        assert isinstance(mapped, gw.DbacsProtocolError)
        assert "segmentation-not-supported" in str(mapped)

    def test_a_list_of_results_is_not_mistaken_for_a_failure(self):
        assert not gw._is_failure(
            [(("analog-input", 1), "present-value", None, 1.0)]
        )


class TestChangeOfValue:
    def test_a_notification_reaches_the_callback(self, gateway, state):
        # bacpypes3 hands back one (property, value) pair per notification,
        # not a list of them, and the gateway notifies on more than one
        # property of an object. Getting that shape wrong loses every
        # notification silently, which only polling would paper over.
        async def scenario():
            seen: list[tuple[str, object]] = []
            point = PointRef(0, MEMBERS_BY_KEY["room_temp"])
            accepted = await gateway.subscribe(
                point, lambda p, v: seen.append((p.key, v)), 1800
            )
            assert accepted
            state.notify(23.5)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await gateway.unsubscribe_all()
            return seen

        assert run(scenario()) == [("0_room_temp", 23.5)]

    def test_notifications_about_other_properties_are_ignored(
        self, gateway, state
    ):
        async def scenario():
            seen: list = []
            point = PointRef(0, MEMBERS_BY_KEY["room_temp"])
            await gateway.subscribe(point, lambda p, v: seen.append(v), 1800)
            state.notify("no fault detected", prop="status-flags")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await gateway.unsubscribe_all()
            return seen

        assert run(scenario()) == []

    def test_a_refusal_is_reported_rather_than_raised(self, gateway, state):
        # The gateway allows five subscribers per object and may already have
        # them. That is not a reason to fail a setup.
        state.cov_budget = 0

        async def scenario():
            point = PointRef(0, MEMBERS_BY_KEY["room_temp"])
            return await gateway.subscribe(point, lambda p, v: None, 1800)

        assert run(scenario()) is False

    def test_unsubscribing_gives_the_slots_back(self, gateway):
        async def scenario():
            point = PointRef(0, MEMBERS_BY_KEY["room_temp"])
            await gateway.subscribe(point, lambda p, v: None, 1800)
            assert gateway.subscription_count == 1
            await gateway.unsubscribe_all()
            return gateway.subscription_count

        assert run(scenario()) == 0


class TestDescriptions:
    def test_reads_the_fault_code_from_the_description(self, gateway):
        point = PointRef(1, MEMBERS_BY_KEY["malfunction_code"])
        assert run(gateway.read_description(point)) == "U1"


class TestObjectCount:
    def test_reads_the_array_length_rather_than_the_whole_list(self, gateway):
        # Index 0 of an array property is its length, which is one read
        # instead of the tens of kilobytes a full Object_List segments into.
        assert run(gateway.read_object_list_count()) > 0

    def test_returns_nothing_rather_than_failing_when_unreachable(
        self, gateway, state
    ):
        state.offline = True
        assert run(gateway.read_object_list_count()) is None


async def _no_sleep(_seconds):
    return None


class TestRaisedProtocolFailures:
    """bacpypes3 does not always *return* the failure.

    `read_property_multiple` returns an error object in place of the value,
    which is trap enough. But plain `ReadProperty` raises its
    ErrorRejectAbortNack instead, and a raise escaping `_read_one` skips
    every fallback the caller has.

    The second gateway on this site found it: it answers an Object_List
    read with `AbortPDU: buffer-overflow`, so the whole-array read has to
    fail into the paged read rather than out of the integration.
    """

    def test_an_abort_is_turned_into_our_own_error(self, monkeypatch):
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        state = fake.FakeGatewayState(populated={0})
        fake.attach(gateway, state)

        async def abort(*_args, **_kwargs):
            raise fake.make_abort("buffer-overflow")

        monkeypatch.setattr(gateway._app, "read_property", abort)

        with pytest.raises(gw.DbacsError) as err:
            asyncio.run(gateway._read_one("device,12", "object-list"))
        assert "buffer-overflow" in str(err.value)

    def test_a_whole_object_list_that_aborts_falls_back_to_paging(
        self, monkeypatch
    ):
        """The point of mapping the abort: the slow path must still run."""
        gateway = gw.DbacsGateway("192.0.2.10", device_instance=12)
        state = fake.FakeGatewayState(populated={0, 1})
        fake.attach(gateway, state)

        paged: list[bool] = []
        real_page = gateway._page_object_list

        async def record(device_id):
            paged.append(True)
            return await real_page(device_id)

        monkeypatch.setattr(gateway, "_page_object_list", record)

        original = gateway._app.read_property

        async def abort_whole_array(address, obj, prop, array_index=None,
                                    **kwargs):
            if prop == "object-list" and array_index is None:
                raise fake.make_abort("buffer-overflow")
            return await original(address, obj, prop,
                                  array_index=array_index, **kwargs)

        monkeypatch.setattr(gateway._app, "read_property", abort_whole_array)

        objects = asyncio.run(gateway._read_object_list())
        assert paged == [True]
        assert objects
