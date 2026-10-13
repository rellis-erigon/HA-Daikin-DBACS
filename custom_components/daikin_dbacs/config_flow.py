"""Setting a gateway up, and choosing which of its points to carry."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICE_INSTANCE,
    CONF_HOST,
    CONF_LOCAL_ADDRESS,
    CONF_LOCAL_PORT,
    CONF_OUR_INSTANCE,
    CONF_PORT,
    DEFAULT_COV_LIFETIME,
    DEFAULT_LOCAL_PORT,
    DEFAULT_OUR_INSTANCE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    OPT_COV_LIFETIME,
    OPT_GROUPS,
    OPT_MEMBERS,
    OPT_SCAN_INTERVAL,
    OPT_USE_COV,
)
from .gateway import DbacsGateway, DbacsNotReady, DbacsUnreachable
from .objects import DEFAULT_MEMBERS, READABLE, address_label

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_DEVICE_INSTANCE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=4194302, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_LOCAL_ADDRESS): selector.TextSelector(),
        vol.Optional(
            CONF_LOCAL_PORT, default=DEFAULT_LOCAL_PORT
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(
            CONF_OUR_INSTANCE, default=DEFAULT_OUR_INSTANCE
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=4194302, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)


class DbacsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a gateway, testing it before the entry exists.

    A gateway that cannot be reached, or that will not say what its device
    instance is, fails the form with the reason rather than creating an entry
    that then sits broken.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            data = _clean(user_input)
            self._async_abort_entries_match(
                {CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]}
            )

            gateway = DbacsGateway(
                data[CONF_HOST],
                data[CONF_PORT],
                device_instance=data.get(CONF_DEVICE_INSTANCE),
                local_address=data.get(CONF_LOCAL_ADDRESS),
                local_port=data[CONF_LOCAL_PORT],
                our_instance=data[CONF_OUR_INSTANCE],
            )
            try:
                info = await gateway.connect()
            except DbacsNotReady:
                errors["base"] = "scanning"
            except DbacsUnreachable as err:
                errors["base"] = "cannot_connect"
                description_placeholders["error"] = str(err)
            except Exception as err:  # noqa: BLE001 - surfaced, not hidden
                _LOGGER.exception("unexpected error connecting to the gateway")
                errors["base"] = "unknown"
                description_placeholders["error"] = str(err)
            else:
                await gateway.close()
                data[CONF_DEVICE_INSTANCE] = info.device_instance
                await self.async_set_unique_id(
                    f"{data[CONF_HOST]}:{data[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                title = info.object_name or f"D-BACS {data[CONF_HOST]}"
                return self.async_create_entry(
                    title=title,
                    data=data,
                    options={OPT_MEMBERS: list(DEFAULT_MEMBERS)},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER, user_input or {}
            ),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> DbacsOptionsFlow:
        return DbacsOptionsFlow()


class DbacsOptionsFlow(OptionsFlow):
    """Which points are carried, and how often they are read.

    This is where bulk enable and disable lives, and it works on two axes
    because the requirement does: by member across every group ("give me
    setpoint on everything", "I never want heater status"), and by group
    ("that floor is not in use"). A full gateway publishes 256 groups of 33
    readable points, so what is *not* carried matters more than what is.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean_options(user_input))

        options = self.config_entry.options
        coordinator = getattr(self.config_entry, "runtime_data", None)

        group_options = _group_options(coordinator)
        schema: dict[Any, Any] = {
            vol.Required(
                OPT_MEMBERS,
                default=list(options.get(OPT_MEMBERS) or DEFAULT_MEMBERS),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=member.key,
                            label=f"{member.number:>2}  {member.label}",
                        )
                        for member in READABLE
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }

        if group_options:
            schema[
                vol.Optional(
                    OPT_GROUPS,
                    default=[
                        str(value)
                        for value in (options.get(OPT_GROUPS) or [])
                    ],
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=group_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        schema[
            vol.Optional(
                OPT_SCAN_INTERVAL,
                default=options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                max=3600,
                step=10,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        )
        schema[
            vol.Optional(OPT_USE_COV, default=options.get(OPT_USE_COV, True))
        ] = selector.BooleanSelector()
        schema[
            vol.Optional(
                OPT_COV_LIFETIME,
                default=options.get(OPT_COV_LIFETIME, DEFAULT_COV_LIFETIME),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=300,
                max=86400,
                step=60,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        )

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema)
        )


def _group_options(coordinator) -> list[selector.SelectOptionDict]:
    """The live groups, so the picker offers what exists rather than 256 slots."""
    if coordinator is None:
        return []
    return [
        selector.SelectOptionDict(
            value=str(ac_number), label=address_label(ac_number)
        )
        for ac_number in coordinator.discovery.live_numbers
    ]


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Numbers arrive from the form as floats; entries should hold ints."""
    data = dict(user_input)
    for key in (
        CONF_PORT,
        CONF_LOCAL_PORT,
        CONF_OUR_INSTANCE,
        CONF_DEVICE_INSTANCE,
    ):
        if data.get(key) is not None:
            data[key] = int(data[key])
    data.setdefault(CONF_PORT, DEFAULT_PORT)
    data.setdefault(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT)
    data.setdefault(CONF_OUR_INSTANCE, DEFAULT_OUR_INSTANCE)
    host = data.get(CONF_HOST)
    if isinstance(host, str):
        data[CONF_HOST] = host.strip()
    return data


def _clean_options(user_input: dict[str, Any]) -> dict[str, Any]:
    options = dict(user_input)
    for key in (OPT_SCAN_INTERVAL, OPT_COV_LIFETIME):
        if options.get(key) is not None:
            options[key] = int(options[key])
    if OPT_GROUPS in options:
        options[OPT_GROUPS] = [int(value) for value in options[OPT_GROUPS]]
    return options
