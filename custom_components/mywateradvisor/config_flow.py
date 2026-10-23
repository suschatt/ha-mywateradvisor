"""Config flow for My Water Advisor."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import MwaApiError, MwaAuthError, MwaClient
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_DEVICE_ID,
    CONF_UNIT,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_UNIT,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    UNIT_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Required(CONF_UNIT, default=DEFAULT_UNIT): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=value, label=label)
                    for value, label in UNIT_OPTIONS.items()
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_BACKFILL_DAYS, default=DEFAULT_BACKFILL_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=1, max=MAX_BACKFILL_DAYS, step=1, mode=NumberSelectorMode.BOX
            )
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD})


class MyWaterAdvisorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My Water Advisor."""

    VERSION = 1

    async def _validate(
        self, email: str, password: str, device_id: str
    ) -> dict[str, str]:
        """Try to log in and list meters. Return form errors, if any."""
        client = MwaClient(
            async_get_clientsession(self.hass), email, password, device_id
        )
        try:
            await client.login()
            meters = await client.get_meters()
        except MwaAuthError:
            return {"base": "invalid_auth"}
        except MwaApiError:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while validating credentials")
            return {"base": "unknown"}
        if not meters:
            return {"base": "no_meters"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for portal credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            device_id = str(uuid.uuid4())
            errors = await self._validate(email, user_input[CONF_PASSWORD], device_id)
            if not errors:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_ID: device_id,
                        CONF_UNIT: user_input[CONF_UNIT],
                        CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the portal rejected the password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate(
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                entry.data[CONF_DEVICE_ID],
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )
