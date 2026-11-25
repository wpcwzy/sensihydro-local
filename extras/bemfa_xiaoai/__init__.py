"""Bemfa (Xiaomi/Xiaoai) sync integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

from .bridge import BemfaBridge

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bemfa_xiaoai"
CONF_UID = "uid"
CONF_ENTITIES = "entities"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_UID): cv.string,
                vol.Required(CONF_ENTITIES): cv.entity_ids,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the bridge from configuration.yaml."""
    conf = config.get(DOMAIN)
    if not conf:
        return True
    bridge = BemfaBridge(hass, conf[CONF_UID], conf[CONF_ENTITIES])
    hass.data.setdefault(DOMAIN, {})["bridge"] = bridge
    await hass.async_add_executor_job(bridge.start)
    return True
