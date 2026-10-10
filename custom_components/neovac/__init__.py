"""The NeoVac MyEnergy integration.

Provides energy and water consumption data from NeoVac MyEnergy for
the Home Assistant Energy Dashboard.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import NeoVacApiClient
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import NeoVacCoordinator
from .statistics import NeoVacStatisticsImporter

_LOGGER = logging.getLogger(__name__)

STATISTICS_INTERVAL = timedelta(minutes=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NeoVac MyEnergy from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Create API client with its own session.
    # We intentionally do NOT use HA's shared session because the NeoVac
    # auth flow requires cross-domain cookies (auth.neovac.ch ->
    # myenergy.neovac.ch) which need a CookieJar that persists between
    # requests to different domains.
    client = NeoVacApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
    )

    # Authenticate
    try:
        await client.authenticate()
    except Exception as err:
        await client.close()
        _LOGGER.error("Failed to authenticate with NeoVac: %s", err)
        raise

    # Create coordinator
    coordinator = NeoVacCoordinator(hass, client, entry)

    # Store coordinator for sensor platform and cleanup
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Forward setup to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Hourly long-term statistics (neovac:*), correct even after Home Assistant was off.
    importer = NeoVacStatisticsImporter(hass, client, coordinator.unit_id)
    entry.async_on_unload(
        async_track_time_interval(hass, importer.async_update, STATISTICS_INTERVAL)
    )
    entry.async_on_unload(async_call_later(hass, 60, importer.async_update))

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload a NeoVac config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        coordinator: NeoVacCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coordinator:
            await coordinator.client.close()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update.

    Reload the integration when options change (e.g., scan interval).
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle removal of a config entry."""
    # Cleanup is handled in async_unload_entry
