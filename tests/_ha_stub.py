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
from enum import StrEnum


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


class HomeAssistant:
    """Just enough of hass to be passed around and stored."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.config_entries = _ConfigEntries()


class _ConfigEntries:
    def __init__(self) -> None:
        self.reloaded: list[str] = []
        self.forwarded: list[tuple] = []

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

    def __class_getitem__(cls, _item):
        return cls

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, func) -> None:
        self._unload_callbacks.append(func)


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
    SENSOR = "sensor"


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


class UnitOfTemperature(StrEnum):
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class UnitOfEnergy(StrEnum):
    KILO_WATT_HOUR = "kWh"


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
    )
    _module("homeassistant.core", HomeAssistant=HomeAssistant, callback=callback)
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

    helpers = _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
        CoordinatorEntity=CoordinatorEntity,
    )
    _module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _module(
        "homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=object,
    )
    _selector_module()

    helpers.selector = sys.modules["homeassistant.helpers.selector"]
    helpers.update_coordinator = sys.modules[
        "homeassistant.helpers.update_coordinator"
    ]
    helpers.device_registry = sys.modules["homeassistant.helpers.device_registry"]
