import asyncio
import json
import logging
import os
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

HA_API_URL = "http://supervisor/core/api"
_HA_WS_URL = "ws://supervisor/core/websocket"
_LOW_NOTIFICATION_ID = "battery_sentinel_low"


def _headers():
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


def _clean_name(name: str) -> str:
    """Strip redundant battery suffixes — e.g. 'Hallway Siren Battery level' -> 'Hallway Siren'.
    Only strips trailing occurrences so 'Replace battery now' is left unchanged."""
    cleaned = re.sub(r'\s+battery\s+level\s*$', '', name, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\s+battery\s*$', '', cleaned, flags=re.IGNORECASE).strip()
    return cleaned or name


# ── HA data fetchers ───────────────────────────────────────────────────

async def get_battery_entities():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HA_API_URL}/states",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error("HA API returned %s", resp.status)
                    return []
                states = await resp.json()

        batteries = [
            {
                "entity_id": s["entity_id"],
                "state": s["state"],
                "name": _clean_name(s["attributes"].get("friendly_name", s["entity_id"])),
                "unit": s["attributes"].get("unit_of_measurement", "%"),
            }
            for s in states
            if s.get("attributes", {}).get("device_class") == "battery"
            and s.get("state") not in ("unavailable", "unknown")
        ]

        _LOGGER.info("Found %d battery entities", len(batteries))
        return batteries

    except Exception:
        _LOGGER.exception("Failed to fetch battery entities from HA")
        return []


async def get_entity_metadata():
    """Returns dict of entity_id -> {area, device_id} for all battery entities."""
    template = (
        "{% set ns = namespace(r={}) %}"
        "{% for s in states %}"
        "{% if s.attributes.device_class == 'battery'"
        " and s.state not in ['unavailable','unknown'] %}"
        "{% set ns.r = dict(ns.r, **{s.entity_id: {"
        "'area': area_name(s.entity_id) or '',"
        "'device_id': device_id(s.entity_id) or ''"
        "}}) %}"
        "{% endif %}"
        "{% endfor %}"
        "{{ ns.r | tojson }}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{HA_API_URL}/template",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"template": template},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Template API returned %s for metadata fetch", resp.status)
                    return {}
                return json.loads(await resp.text())
    except Exception:
        _LOGGER.exception("Failed to fetch entity metadata")
        return {}


async def get_scripts() -> list:
    """Returns sorted list of {entity_id, name} for all HA scripts."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HA_API_URL}/states",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                states = await resp.json()
        return sorted(
            [
                {
                    "entity_id": s["entity_id"],
                    "name": s["attributes"].get("friendly_name", s["entity_id"]),
                }
                for s in states
                if s["entity_id"].startswith("script.")
            ],
            key=lambda x: x["name"].lower(),
        )
    except Exception:
        _LOGGER.exception("Failed to fetch scripts")
        return []


async def get_notify_services() -> list:
    """Returns sorted list of service slugs under the HA notify domain."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HA_API_URL}/services",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                all_services = await resp.json()
        for domain_obj in all_services:
            if domain_obj.get("domain") == "notify":
                return sorted(domain_obj.get("services", {}).keys())
        return []
    except Exception:
        _LOGGER.exception("Failed to fetch notify services")
        return []


# ── Helpers ────────────────────────────────────────────────────────────

def device_is_low(device: dict) -> bool:
    """True if the device is currently below its alert threshold."""
    threshold = device.get("alert_threshold", 15)
    if threshold == -1:
        return False
    if device["entity_id"].startswith("binary_sensor."):
        return device["state"] == "on"
    try:
        return int(device["state"]) < threshold
    except (ValueError, TypeError):
        return False


def level_str(device: dict) -> str:
    if device["entity_id"].startswith("binary_sensor."):
        return "Low" if device["state"] == "on" else "OK"
    return f"{device['state']}%"


def _report_sort_key(device: dict) -> int:
    """Sort key: binary 'on' (low) = 0, numeric ascending, binary 'off' = 101."""
    if device["entity_id"].startswith("binary_sensor."):
        return 0 if device["state"] == "on" else 101
    try:
        return int(device["state"])
    except (ValueError, TypeError):
        return 100


def _format_line(device: dict, include_type: bool) -> str:
    area  = f" ({device['area']})" if device.get("area") else ""
    btype = f" [{device['battery_type']}]" if include_type and device.get("battery_type") else ""
    return f"- {device['name']}{area}: {level_str(device)}{btype}"


# ── Consolidated low-battery UI notification ───────────────────────────

async def update_low_battery_notification(devices: list, settings: dict):
    """Create/update or dismiss the single persistent low-battery notification."""
    if not settings.get("notify_persistent", True):
        return

    include_type = settings.get("report_include_battery_type", False)
    low = sorted([d for d in devices if device_is_low(d)], key=_report_sort_key)

    if not low:
        await _dismiss_persistent(_LOW_NOTIFICATION_ID)
        return

    _LOGGER.info("Low battery notification: %d device(s) below threshold", len(low))
    lines = [_format_line(d, include_type) for d in low]
    await _fire_persistent(
        "Battery Sentinel: Low Batteries",
        "\n".join(lines),
        notification_id=_LOW_NOTIFICATION_ID,
    )


# ── Per-device email/mobile alert (fires once on threshold crossing) ───

async def fire_low_battery_email(title: str, message: str, settings: dict, device: dict):
    """Email + mobile alert for a device that just crossed its threshold.
    Bell is intentionally excluded — that is handled by the consolidated notification."""
    service = settings.get("notify_email_service", "").strip()
    if device.get("notify_email", True) and service:
        addr = device.get("notify_email_address", "") or settings.get("notify_email_to", "")
        targets = [a.strip() for a in addr.split(",") if a.strip()] if addr else []
        cc = settings.get("notify_email_cc", "")
        if cc:
            targets.extend(a.strip() for a in cc.split(",") if a.strip())
        if targets:
            await _fire_notify_service(service, title, message, targets)

    if device.get("notify_mobile", False):
        mobile = (device.get("notify_mobile_service", "").strip()
                  or settings.get("notify_mobile_default_service", "").strip())
        if mobile:
            await _fire_notify_service(mobile.removeprefix("notify."), title, message, [])


# ── General notification (bell + email — new device alerts, etc.) ──────

async def fire_notification(title: str, message: str, settings: dict, device: dict = None, use_bell: bool = True):
    """Fire bell and/or email. Used for new-device alerts and daily report."""
    use_bell  = use_bell and (device.get("notify_bell",  True) if device else settings.get("notify_persistent", True))
    use_email = device.get("notify_email", True) if device else True

    if use_bell:
        await _fire_persistent(title, message)

    service = settings.get("notify_email_service", "").strip()
    if use_email and service:
        addr = (device.get("notify_email_address", "") if device else "") \
               or settings.get("notify_email_to", "")
        targets = [a.strip() for a in addr.split(",") if a.strip()] if addr else []
        cc = settings.get("notify_email_cc", "")
        if cc:
            targets.extend(a.strip() for a in cc.split(",") if a.strip())
        if targets:
            await _fire_notify_service(service, title, message, targets)

    if device:
        mobile = device.get("notify_mobile_service", "").strip()
        if mobile:
            await _fire_notify_service(mobile.removeprefix("notify."), title, message, [])


# ── Daily report ───────────────────────────────────────────────────────

async def send_daily_report(devices: list, settings: dict):
    """Build and send the daily battery report email."""
    include_all  = settings.get("daily_report_include_all", False)
    include_type = settings.get("report_include_battery_type", False)

    report_devices = list(devices) if include_all else [d for d in devices if device_is_low(d)]
    report_devices = sorted(report_devices, key=_report_sort_key)

    if not report_devices:
        _LOGGER.info("Daily report: nothing to report, skipping send")
        return

    lines = [_format_line(d, include_type) for d in report_devices]
    await fire_notification(
        "Battery Sentinel: Daily Report",
        "\n".join(lines),
        settings,
        use_bell=False,
    )
    _LOGGER.info("Daily report sent (%d device(s))", len(report_devices))


# ── Script trigger ────────────────────────────────────────────────────

async def fire_script(script_entity_id: str, device: dict):
    """Fire a HA script with battery device context passed as variables."""
    variables = {
        "device_name":   device.get("name", ""),
        "battery_level": level_str(device),
        "battery_type":  device.get("battery_type", ""),
        "area":          device.get("area", ""),
        "entity_id":     device["entity_id"],
    }
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/script/turn_on",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"entity_id": script_entity_id, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Script '%s' fired for %s", script_entity_id, device["entity_id"])
    except Exception:
        _LOGGER.exception("Failed to fire script '%s' for %s", script_entity_id, device["entity_id"])


# ── Entity registry ───────────────────────────────────────────────────

async def rename_entity(entity_id: str, new_name: str) -> bool:
    """Rename an entity's friendly name via the HA WebSocket API."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_HA_WS_URL) as ws:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                _LOGGER.info("WS rename: step1 type=%s", msg.get("type"))
                if msg.get("type") != "auth_required":
                    _LOGGER.warning("WS rename: expected auth_required, got %s", msg)
                    return False
                await ws.send_json({"type": "auth", "access_token": token})
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                _LOGGER.info("WS rename: step2 type=%s", msg.get("type"))
                if msg.get("type") != "auth_ok":
                    _LOGGER.warning("WS rename: auth failed: %s", msg)
                    return False
                await ws.send_json({
                    "id": 1,
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                    "name": new_name or None,
                })
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                _LOGGER.info("WS rename: result = %s", msg)
                if not msg.get("success"):
                    _LOGGER.warning("WS rename: update failed: %s", msg)
                    return False
                return True
    except asyncio.TimeoutError:
        _LOGGER.error("WS rename timed out for %s", entity_id)
        return False
    except Exception:
        _LOGGER.exception("Failed to rename entity %s via WebSocket", entity_id)
        return False


# ── Primitives ─────────────────────────────────────────────────────────

async def _fire_persistent(title: str, message: str, notification_id: str = None):
    payload = {"title": title, "message": message}
    if notification_id:
        payload["notification_id"] = notification_id
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/persistent_notification/create",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Persistent notification fired: %s", title)
    except Exception:
        _LOGGER.exception("Failed to fire persistent notification")


async def _dismiss_persistent(notification_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/persistent_notification/dismiss",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"notification_id": notification_id},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Persistent notification dismissed: %s", notification_id)
    except Exception:
        _LOGGER.exception("Failed to dismiss persistent notification %s", notification_id)


async def _fire_notify_service(service: str, title: str, message: str, targets: list):
    payload = {"title": title, "message": message}
    if targets:
        payload["target"] = targets
    if not service.startswith("mobile_app_"):
        html_body = "<br>".join(message.split("\n"))
        payload["data"] = {"html": f"<html><body>{html_body}</body></html>"}
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{HA_API_URL}/services/notify/{service}",
                headers={**_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        _LOGGER.info("Notify service '%s' fired: %s", service, title)
    except Exception:
        _LOGGER.exception("Failed to fire notify service '%s'", service)
