"""My Water Advisor: import hourly water usage into Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LoginGuard, MwaClient
from .const import CONF_DEVICE_ID
from .coordinator import MwaCoordinator, login_store

type MwaConfigEntry = ConfigEntry[MwaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MwaConfigEntry) -> bool:
    """Set up My Water Advisor from a config entry."""
    # No login here: a failed login in setup makes Home Assistant retry setup
    # every minute or so, forever, which is exactly what locks an account.
    # The coordinator logs in on its first refresh, under the login guard.
    store = login_store(hass, entry.entry_id)
    guard = LoginGuard(
        await store.async_load(),
        on_change=lambda data: store.async_delay_save(lambda: data, 1),
    )
    client = MwaClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_DEVICE_ID],
        guard=guard,
    )

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


async def async_remove_entry(hass: HomeAssistant, entry: MwaConfigEntry) -> None:
    """Delete the saved login state with the entry."""
    await login_store(hass, entry.entry_id).async_remove()
