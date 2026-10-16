"""Home Assistant notification backend.

Sends the notification through a Home Assistant notify service (typically the
Companion app: ``notify.mobile_app_<device>``), so users don't need ntfy or any
extra app.

Thumbnails: the Companion app fetches ``data.image`` itself, and Immich's
thumbnail endpoint needs an API key, so instead the JPEG is written into Home
Assistant's ``www`` folder (served at ``/local/``) under a random file name and
removed again after ``image_ttl_hours``. Anything under ``/local/`` is served
without authentication, so the random name is what keeps the URL private.
"""

import logging
import os
import re
import time
import uuid
from pathlib import Path

import requests

from .immich import fetch_thumbnail
from .utils import with_retry

_FILE_RE = re.compile(r"^[0-9a-f]{32}\.jpg$")


def _service_path(target: str) -> str:
    """Turn 'notify.mobile_app_x' / 'mobile_app_x' into the REST path segment."""
    target = target.strip()
    if target.startswith("notify."):
        target = target[len("notify."):]
    if not re.fullmatch(r"[a-z0-9_]+", target):
        raise ValueError(f"Invalid Home Assistant notify service: {target!r}")
    return target


def _cleanup_old_images(image_dir: Path, ttl_hours: float, logger: logging.Logger) -> None:
    """Delete images we created that are older than the TTL (our own files only)."""
    cutoff = time.time() - ttl_hours * 3600
    try:
        for entry in image_dir.iterdir():
            if _FILE_RE.match(entry.name) and entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError as e:
        logger.debug(f"Home Assistant image cleanup skipped: {e}")


def _store_image(ha_cfg: dict, data: bytes, logger: logging.Logger) -> str | None:
    """Write the JPEG into HA's www folder and return its /local/ URL path."""
    www_path = ha_cfg.get("www_path")
    if not www_path:
        logger.warning("homeassistant.www_path not set — sending without thumbnail")
        return None

    subdir = ha_cfg.get("image_subdir") or "memnotify"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", subdir):
        logger.warning(f"Invalid homeassistant.image_subdir {subdir!r} — sending without thumbnail")
        return None

    image_dir = Path(www_path) / subdir
    try:
        image_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_images(image_dir, float(ha_cfg.get("image_ttl_hours", 24)), logger)
        name = f"{uuid.uuid4().hex}.jpg"
        (image_dir / name).write_bytes(data)
    except OSError as e:
        logger.warning(f"Could not write thumbnail to {image_dir}: {e} — sending without thumbnail")
        return None
    return f"/local/{subdir}/{name}"


def send_single_notification(
    user: dict,
    notification: dict,
    config: dict,
    logger: logging.Logger,
    thumbnail_override: bytes = None,
) -> bool:
    """Send a notification via a Home Assistant notify service."""
    name = user["name"]
    asset_id = notification.get("asset_id")

    ha_cfg = config.get("homeassistant") or {}
    ha_url = (ha_cfg.get("url") or "").rstrip("/")
    token = ha_cfg.get("token") or ""
    target = user.get("ha_notify_service") or ""

    if not ha_url or not token:
        logger.error(f"  [{name}] homeassistant.url / homeassistant.token not configured")
        return False
    if not target:
        logger.error(f"  [{name}] No ha_notify_service configured (e.g. notify.mobile_app_pixel_8)")
        return False
    try:
        service = _service_path(target)
    except ValueError as e:
        logger.error(f"  [{name}] {e}")
        return False

    immich_url = config["immich"]["url"]
    click_base = config["immich"].get("external_url") or ""
    if click_base and not click_base.startswith(("http://", "https://")):
        click_base = f"https://{click_base}"
    retry_config = config["settings"]["retry"]
    api_key = user["immich_api_key"]

    # Fetch thumbnail (same selection logic as the ntfy/apprise backends)
    thumbnail_data = None
    if thumbnail_override and not asset_id:
        thumbnail_data = thumbnail_override
    elif asset_id:
        try:
            thumbnail_data = with_retry(
                lambda: fetch_thumbnail(immich_url, api_key, asset_id),
                max_attempts=retry_config["max_attempts"],
                delay=retry_config["delay_seconds"],
                logger=logger,
            )
            logger.debug(f"  [{name}] Thumbnail: {len(thumbnail_data):,} bytes")
        except Exception as e:
            logger.warning(f"  [{name}] Could not fetch thumbnail: {e}")
            if thumbnail_override:
                thumbnail_data = thumbnail_override
    elif thumbnail_override:
        thumbnail_data = thumbnail_override

    click_url = notification.get("click_url")
    if not click_url and click_base:
        click_url = f"{click_base}/photos/{asset_id}" if asset_id else f"{click_base}/"

    # No "tag": on Android a tag makes each notification replace the previous one,
    # and up to one notification per window is sent per day.
    data = {"channel": "Immich Memories"}
    if click_url:
        data["clickAction"] = click_url  # Android
        data["url"] = click_url          # iOS
    if thumbnail_data:
        image_path = _store_image(ha_cfg, thumbnail_data, logger)
        if image_path:
            data["image"] = image_path

    payload = {
        "title": notification["title"],
        "message": notification["message"],
        "data": data,
    }
    headers = {"Authorization": f"Bearer {token}"}
    endpoint = f"{ha_url}/api/services/notify/{service}"

    def _post() -> bool:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return True

    try:
        return with_retry(
            _post,
            max_attempts=retry_config["max_attempts"],
            delay=retry_config["delay_seconds"],
            logger=logger,
        )
    except Exception as e:
        logger.error(f"  [{name}] Home Assistant notification failed: {e}")
        return False
