"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mywateradvisor.api import (
    Meter,
    MwaApiError,
    MwaAuthError,
    MwaLoginRefusedError,
)
from custom_components.mywateradvisor.const import (
    CONF_BACKFILL_DAYS,
    CONF_DEVICE_ID,
    CONF_UNIT,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

API = "custom_components.mywateradvisor.config_flow.MwaClient"
METER = Meter(meter_count=591, serial="221370217", address="1 Example St")
USER_INPUT = {
    CONF_EMAIL: "Someone@Example.com",
    CONF_PASSWORD: "secret",
    CONF_UNIT: "gal",
    CONF_BACKFILL_DAYS: 30,
}


@pytest.fixture(autouse=True)
def no_setup():
    """Don't run the real setup after a flow creates an entry."""
    with patch("custom_components.mywateradvisor.async_setup_entry", return_value=True):
        yield


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(f"{API}.login"), patch(f"{API}.get_meters", return_value=[METER]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Someone@Example.com"
    data = result["data"]
    assert data[CONF_PASSWORD] == "secret"
    assert data[CONF_UNIT] == "gal"
    assert data[CONF_BACKFILL_DAYS] == 30
    assert data[CONF_DEVICE_ID]
    assert result["result"].unique_id == "someone@example.com"


@pytest.mark.parametrize(
    ("side_effect", "meters", "error"),
    [
        (MwaAuthError("no"), [], "invalid_auth"),
        (MwaLoginRefusedError("no", 400, None), [], "login_refused"),
        (MwaApiError("down"), [], "cannot_connect"),
        (None, [], "no_meters"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, side_effect, meters, error) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(f"{API}.login", side_effect=side_effect), patch(
        f"{API}.get_meters", return_value=meters
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    # The user can fix it and finish.
    with patch(f"{API}.login"), patch(f"{API}.get_meters", return_value=[METER]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_account_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="someone@example.com", data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone@example.com",
        data={
            CONF_EMAIL: "someone@example.com",
            CONF_PASSWORD: "old",
            CONF_DEVICE_ID: "dev",
            CONF_UNIT: "gal",
            CONF_BACKFILL_DAYS: 30,
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(f"{API}.login", side_effect=MwaAuthError("no")), patch(f"{API}.get_meters"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "still-wrong"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(f"{API}.login"), patch(f"{API}.get_meters", return_value=[METER]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="someone@example.com", data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_hours": 12, "hour_label": "end"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"poll_hours": 12, "hour_label": "end"}
