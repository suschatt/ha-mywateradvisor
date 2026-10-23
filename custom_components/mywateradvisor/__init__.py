"""My Water Advisor: import hourly water usage into Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MwaApiError, MwaAuthError, MwaClient
from .const import CONF_DEVICE_ID
from .coordinator import MwaCoordinator

type MwaConfigEntry = ConfigEntry[MwaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MwaConfigEntry) -> bool:
    """Set up My Water Advisor from a config entry."""
    client = MwaClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_DEVICE_ID],
    )
    try:
        await client.login()
    except MwaAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except MwaApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = MwaCoordinator(hass, entry, client)
    entry.runtime_data = coordinator

    # A first-time backfill can take a minute or more, so run it in the
    # background instead of holding up Home Assistant's startup.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "mywateradvisor_initial_import"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MwaConfigEntry) -> bool:
    """Unload a config entry. Imported statistics are kept."""
    return True
