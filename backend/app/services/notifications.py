"""Best-effort Expo push delivery for registered mobile devices."""

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device

logger = logging.getLogger(__name__)
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def is_expo_push_token(token: str | None) -> bool:
    return bool(token and (token.startswith("ExpoPushToken[") or token.startswith("ExponentPushToken[")))


async def send_device_notification(
    session: AsyncSession,
    device: Device,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    if not is_expo_push_token(device.push_token):
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(EXPO_PUSH_URL, json={
                "to": device.push_token,
                "title": title,
                "body": body,
                "sound": "default",
                "priority": "high",
                "data": data or {},
            })
            response.raise_for_status()
            entries = response.json().get("data", [])
            if entries and entries[0].get("details", {}).get("error") == "DeviceNotRegistered":
                device.push_token = None
                device.push_token_updated_at = None
                await session.commit()
    except Exception as exc:
        # Notification outages must never turn a successful backup or recovery
        # into a failed operation.
        logger.warning("push_delivery_failed", extra={"device_id": str(device.id), "reason": str(exc)[:240]})


async def notify_user_devices(
    session: AsyncSession,
    user_id: object,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    devices = list((await session.scalars(select(Device).where(Device.user_id == user_id, Device.push_token.is_not(None)))).all())
    for device in devices:
        await send_device_notification(session, device, title=title, body=body, data=data)
