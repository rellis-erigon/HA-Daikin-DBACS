"""Minimal Home Assistant stand-ins, installed only when HA is absent.

CI installs the real `homeassistant` package and none of this loads. It
exists so the suite also runs inside a container and on a machine without a
400 MB dependency tree — the alternative is tests nobody runs locally, which
is how an enumeration gets mislabelled all the way to a live building.

Only what the integration actually imports is stubbed. Anything beyond that
should fail loudly rather than be quietly faked.
"""

from __future__ import annotations

import sys
import types
from enum import IntFlag, StrEnum


def homeassistant_available() -> bool:
    try:
        import homeassistant  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _module(name: str, **attrs) -> types.ModuleType:
    """Register a stub module and wire it onto its parent."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    if "." in name:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, module)
    return module


def callback(func):
    return func


class _Loop:
    """The event loop's monotonic clock, which the write guard reads.

    Settable, so a test can cover a day of write-rate behaviour without
    waiting one.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SupportsResponse(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    ONLY = "only"


class _States:
    """The state machine, only as far as removing a state."""

    def __init__(self) -> None:
        self.removed: list = []

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)


class HomeAssistant:
    """Just enough of hass to be passed around and stored."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.config_entries = _ConfigEntries()
        self.loop = _Loop()
        self.states = _States()
        self.services = _Services()

    async def async_add_executor_job(self, func, *args):
        """Home Assistant's loop guard is right that file I/O blocks."""
        return func(*args)


class _Services:
    """Just enough of the service registry to register and call one."""

    def __init__(self) -> None:
        self.registered: dict = {}

    def has_service(self, domain, service) -> bool:
        return (domain, service) in self.registered

    def async_register(self, domain, service, handler, **_kwargs) -> None:
        self.registered[(domain, service)] = handler

    async def async_call(self, domain, service, data=None) -> None:
        await self.registered[(domain, service)](_ServiceCall(data or {}))


class _ServiceCall:
    def __init__(self, data: dict) -> None:
        self.data = data


class _ConfigEntries:
    def __init__(self) -> None:
        self.reloaded: list[str] = []
        self.forwarded: list[tuple] = []
        self.entries: list = []

    def async_entries(self, _domain=None) -> list:
        return list(self.entries)

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        self.forwarded.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True


class ConfigEntry:
    """A config entry that can be subscripted, so PEP 695 aliases work."""

    def __init__(
        self,
        data: dict | None = None,
        options: dict | None = None,
        entry_id: str = "test_entry",
    ) -> None:
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.runtime_data = None
        self._unload_callbacks: list = []
        self.background_tasks: list = []

    def __class_getitem__(cls, _item):
        return cls

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, func) -> None:
        self._unload_callbacks.append(func)

    def async_create_background_task(self, hass, coro, name=None):
        import asyncio

        task = asyncio.ensure_future(coro)
        self.background_tasks.append((name, task))
        return task


class ConfigFlow:
    """Enough of the flow machinery to drive a step and read its result."""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__()

    def __init__(self) -> None:
        self.unique_id: str | None = None

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs) -> dict:
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs) -> dict:
        return {"type": "abort", **kwargs}

    def add_suggested_values_to_schema(self, schema, _values):
        return schema

    def _async_abort_entries_match(self, _match) -> None:
        return None

    async def async_set_unique_id(self, unique_id) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        return None


class OptionsFlow:
    def __init__(self) -> None:
        self.config_entry: ConfigEntry | None = None

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs) -> dict:
        return {"type": "create_entry", **kwargs}


class UpdateFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class DataUpdateCoordinator:
    """A coordinator that records rather than schedules.

    Tests drive `_async_update_data` directly; nothing here starts a timer,
    because a test that waits on a real interval is a test that gets deleted.
    """

    def __init__(
        self,
        hass,
        logger,
        *,
        name: str = "",
        update_interval=None,
        config_entry=None,
        **kwargs,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True
        self.updates: list = []

    def __class_getitem__(cls, _item):
        return cls

    async def async_refresh(self) -> None:
        self.data = await self._async_update_data()

    async def async_config_entry_first_refresh(self) -> None:
        await self.async_refresh()

    async def async_request_refresh(self) -> None:
        await self.async_refresh()

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.updates.append(data)

    async def _async_update_data(self):
        raise NotImplementedError


class CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, _item):
        return cls


class _Entity:
    """A stand-in for the platform entity base classes."""

    _attr_has_entity_name = False
    _attr_name = None
    _attr_unique_id = None
    _attr_device_info = None
    _attr_device_class = None
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_options = None
    _attr_suggested_display_precision = None
    _attr_entity_registry_enabled_default = True

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def device_class(self):
        return self._attr_device_class

    @property
    def native_unit_of_measurement(self):
        return self._attr_native_unit_of_measurement

    @property
    def state_class(self):
        return self._attr_state_class

    @property
    def options(self):
        return self._attr_options

    @property
    def entity_registry_enabled_default(self):
        return self._attr_entity_registry_enabled_default


class Platform(StrEnum):
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    CLIMATE = "climate"
    EVENT = "event"
    SENSOR = "sensor"
    SWITCH = "switch"


class SensorDeviceClass(StrEnum):
    TEMPERATURE = "temperature"
    ENERGY = "energy"
    ENUM = "enum"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(StrEnum):
    PROBLEM = "problem"
    RUNNING = "running"


class HVACMode(StrEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class HVACAction(StrEnum):
    OFF = "off"
    IDLE = "idle"
    HEATING = "heating"
    COOLING = "cooling"
    DRYING = "drying"
    FAN = "fan"


class ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    SWING_MODE = 32
    TURN_OFF = 128
    TURN_ON = 256


class _ClimateEntity(_Entity):
    _attr_temperature_unit = None
    _attr_target_temperature_step = None
    _attr_min_temp = None
    _attr_max_temp = None
    _attr_hvac_modes = None
    _attr_fan_modes = None
    _attr_swing_modes = None
    _attr_supported_features = 0
    _attr_translation_key = None

    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def hvac_modes(self):
        return self._attr_hvac_modes

    @property
    def fan_modes(self):
        return self._attr_fan_modes

    @property
    def swing_modes(self):
        return self._attr_swing_modes


class _EventEntity(_Entity):
    _attr_event_types = []
    _attr_translation_key = None

    @property
    def events(self) -> list:
        """What has been fired. Lazy, because the subclass owns __init__."""
        if getattr(self, "_fired", None) is None:
            self._fired: list = []
        return self._fired

    @property
    def event_types(self):
        return self._attr_event_types

    def _trigger_event(self, event_type, attributes=None) -> None:
        self.events.append((event_type, attributes or {}))

    async def async_added_to_hass(self) -> None:
        return None

    def _handle_coordinator_update(self) -> None:
        return None

    def async_write_ha_state(self) -> None:
        return None


class UnitOfTemperature(StrEnum):
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class UnitOfEnergy(StrEnum):
    KILO_WATT_HOUR = "kWh"


class _DeviceEntry:
    def __init__(self, identifiers, device_id="hub_device_id") -> None:
        self.id = device_id
        self.identifiers = identifiers
        self.name_by_user = None
        self.area_id = None


class _DeviceRegistry:
    """Records what was registered, so a test can check the hub exists."""

    def __init__(self) -> None:
        self.created: list = []
        self.devices: dict = {}
        self.updated: list = []

    def async_get_or_create(self, **kwargs):
        entry = _DeviceEntry(kwargs.get("identifiers"))
        self.created.append(kwargs)
        return entry

    def add_device(self, device_id, identifiers):
        self.devices[device_id] = _DeviceEntry(identifiers)
        return self.devices[device_id]

    def async_get(self, device_id):
        return self.devices.get(device_id)

    def async_get_device(self, identifiers=None, **_kwargs):
        for device in self.devices.values():
            if identifiers and set(identifiers) & set(device.identifiers or ()):
                return device
        return None

    def async_update_device(self, device_id, **changes):
        device = self.devices.get(device_id)
        if device is None:
            return None
        for key, value in changes.items():
            setattr(device, key, value)
        self.updated.append((device_id, changes))
        return device


_REGISTRIES: dict = {}


def _device_registry(hass):
    return _REGISTRIES.setdefault(id(hass), _DeviceRegistry())


class RegistryEntryDisabler(StrEnum):
    INTEGRATION = "integration"
    USER = "user"
    CONFIG_ENTRY = "config_entry"


class _RegistryEntry:
    def __init__(self, entity_id, unique_id, disabled_by=None) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.disabled_by = disabled_by


class _Area:
    def __init__(self, name, area_id) -> None:
        self.name = name
        self.id = area_id


class _AreaRegistry:
    def __init__(self) -> None:
        self.areas: list = []

    def add(self, name) -> _Area:
        area = _Area(name, name.lower().replace(" ", "_").replace("-", "_"))
        self.areas.append(area)
        return area

    def async_list_areas(self) -> list:
        return list(self.areas)

    def async_create(self, name) -> _Area:
        return self.add(name)


_AREA_REGISTRIES: dict = {}


def _area_registry(hass):
    return _AREA_REGISTRIES.setdefault(id(hass), _AreaRegistry())


class _EntityRegistry:
    """Just the enable/disable surface the integration reconciles against."""

    def __init__(self) -> None:
        self.entries: list = []
        self.updates: list = []

    def add(self, entity_id, unique_id, disabled_by=None) -> _RegistryEntry:
        entry = _RegistryEntry(entity_id, unique_id, disabled_by)
        self.entries.append(entry)
        return entry

    def async_get_entity_id(self, domain, platform, unique_id):
        for entry in self.entries:
            if entry.unique_id == unique_id and entry.entity_id.startswith(
                domain + "."
            ):
                return entry.entity_id
        return None

    def async_update_entity(self, entity_id, **changes):
        for entry in self.entries:
            if entry.entity_id == entity_id:
                for key, value in changes.items():
                    setattr(entry, key, value)
                self.updates.append((entity_id, changes))
                return entry
        return None


_ENTITY_REGISTRIES: dict = {}


def _entity_registry(hass):
    return _ENTITY_REGISTRIES.setdefault(id(hass), _EntityRegistry())


def _entries_for_config_entry(registry, _entry_id):
    return list(registry.entries)


def _selector_module() -> types.ModuleType:
    """Selectors that only have to remember what they were given."""

    class _Selector:
        def __init__(self, config=None) -> None:
            self.config = config

        def __call__(self, value):
            return value

    class _Config(dict):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            for key, value in kwargs.items():
                setattr(self, key, value)

    class SelectSelectorMode(StrEnum):
        LIST = "list"
        DROPDOWN = "dropdown"

    class NumberSelectorMode(StrEnum):
        BOX = "box"
        SLIDER = "slider"

    return _module(
        "homeassistant.helpers.selector",
        TextSelector=_Selector,
        TextSelectorConfig=_Config,
        NumberSelector=_Selector,
        NumberSelectorConfig=_Config,
        NumberSelectorMode=NumberSelectorMode,
        BooleanSelector=_Selector,
        SelectSelector=_Selector,
        SelectSelectorConfig=_Config,
        SelectSelectorMode=SelectSelectorMode,
        SelectOptionDict=dict,
    )


def install() -> None:
    """Install the stubs, unless the real package is importable."""
    if homeassistant_available():
        return

    _module("homeassistant")
    _module(
        "homeassistant.const",
        Platform=Platform,
        UnitOfTemperature=UnitOfTemperature,
        UnitOfEnergy=UnitOfEnergy,
        ATTR_TEMPERATURE="temperature",
    )
    _module(
        "homeassistant.core",
        HomeAssistant=HomeAssistant,
        callback=callback,
        SupportsResponse=_SupportsResponse,
    )
    _module(
        "homeassistant.config_entries",
        ConfigEntry=ConfigEntry,
        ConfigFlow=ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlow=OptionsFlow,
    )
    _module(
        "homeassistant.exceptions",
        ConfigEntryNotReady=ConfigEntryNotReady,
        HomeAssistantError=HomeAssistantError,
    )
    _module("homeassistant.data_entry_flow", FlowResult=dict)

    _module("homeassistant.components")
    _module(
        "homeassistant.components.sensor",
        SensorEntity=_Entity,
        SensorDeviceClass=SensorDeviceClass,
        SensorStateClass=SensorStateClass,
    )
    _module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=_Entity,
        BinarySensorDeviceClass=BinarySensorDeviceClass,
    )
    _module("homeassistant.components.button", ButtonEntity=_Entity)
    _module("homeassistant.components.switch", SwitchEntity=_Entity)
    _module("homeassistant.components.event", EventEntity=_EventEntity)
    _module(
        "homeassistant.components.climate",
        ClimateEntity=_ClimateEntity,
        ClimateEntityFeature=ClimateEntityFeature,
        HVACAction=HVACAction,
        HVACMode=HVACMode,
    )

    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
        CoordinatorEntity=CoordinatorEntity,
    )
    _module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
        async_get=_device_registry,
    )
    _module(
        "homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=object,
    )
    _module(
        "homeassistant.helpers.area_registry",
        async_get=_area_registry,
    )
    _module(
        "homeassistant.helpers.entity_registry",
        async_get=_entity_registry,
        async_entries_for_config_entry=_entries_for_config_entry,
        RegistryEntryDisabler=RegistryEntryDisabler,
    )
    _selector_module()

    helpers.area_registry = sys.modules["homeassistant.helpers.area_registry"]
    helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
    helpers.selector = sys.modules["homeassistant.helpers.selector"]
    helpers.update_coordinator = sys.modules[
        "homeassistant.helpers.update_coordinator"
    ]
    helpers.device_registry = sys.modules["homeassistant.helpers.device_registry"]
