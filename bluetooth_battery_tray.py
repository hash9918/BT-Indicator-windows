"""Bluetooth Battery Tray

Windows 10/11 tray application that monitors battery levels for connected Bluetooth devices.

Features:
- Pystray system tray icon with dynamic battery percentage text
- BLE battery polling via bleak (Battery Service 0x180F / Characteristic 0x2A19)
- Low-battery notification (<20%) once per discharge cycle
- Right-click menu with current device list, Refresh, and Exit
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as Item
from bleak import BleakClient, BleakScanner

try:
    from win10toast import ToastNotifier
except Exception:  # pragma: no cover - optional import guard for packaging/runtime
    ToastNotifier = None

# BLE Battery Service and Characteristic UUIDs
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Thresholds
LOW_BATTERY_THRESHOLD = 20
RESET_NOTIFY_THRESHOLD = 25

# Polling intervals
FAST_POLL_SECONDS = 60
SLOW_POLL_SECONDS = 180


@dataclass
class DeviceBattery:
    name: str
    address: str
    battery_percent: int
    connected: bool = True
    last_seen: datetime = field(default_factory=datetime.utcnow)


class NotificationManager:
    """Manages one-shot low-battery notifications per discharge cycle."""

    def __init__(self) -> None:
        self._notifier = ToastNotifier() if ToastNotifier else None
        self._notified_low: Dict[str, bool] = {}

    def maybe_notify(self, device: DeviceBattery) -> None:
        """Notify once when device battery crosses below threshold."""
        key = device.address
        already_notified = self._notified_low.get(key, False)

        if device.battery_percent < LOW_BATTERY_THRESHOLD and not already_notified:
            self._show_notification(
                title="Bluetooth Battery Low",
                message=f"{device.name}: {device.battery_percent}% remaining",
            )
            self._notified_low[key] = True
        elif device.battery_percent >= RESET_NOTIFY_THRESHOLD:
            # Reset when the battery has recovered to avoid spam while still low.
            self._notified_low[key] = False

    def _show_notification(self, title: str, message: str) -> None:
        if self._notifier:
            try:
                self._notifier.show_toast(title, message, duration=6, threaded=True)
                return
            except Exception:
                logging.exception("Toast notification failed")

        # Fallback to a console log if toast is unavailable.
        logging.warning("%s - %s", title, message)


class BluetoothBatteryService:
    """Discovers BLE devices and reads battery percentages when available."""

    async def get_connected_battery_devices(self) -> List[DeviceBattery]:
        """Scan for nearby BLE devices and query battery service when exposed.

        Note: Windows reports many connected HID devices as BLE peripherals exposing
        Battery Service. Classic Bluetooth devices without GATT battery support
        cannot be queried via bleak.
        """
        devices = await BleakScanner.discover(timeout=6.0)
        results: List[DeviceBattery] = []

        for dev in devices:
            name = (dev.name or "Unknown Device").strip()
            address = dev.address
            if not address:
                continue

            battery = await self._read_battery_level(address)
            if battery is None:
                continue

            results.append(
                DeviceBattery(
                    name=name,
                    address=address,
                    battery_percent=battery,
                    connected=True,
                )
            )

        # De-duplicate by address (keep latest reading)
        dedup: Dict[str, DeviceBattery] = {d.address: d for d in results}
        return sorted(dedup.values(), key=lambda d: d.name.lower())

    async def _read_battery_level(self, address: str) -> Optional[int]:
        client = BleakClient(address, timeout=10.0)
        try:
            await client.connect()
            services = await client.get_services()

            has_battery_service = any(
                s.uuid.lower() == BATTERY_SERVICE_UUID for s in services
            )
            if not has_battery_service:
                return None

            data = await client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
            if not data:
                return None

            level = int(data[0])
            if 0 <= level <= 100:
                return level
            return None
        except Exception:
            # Device may reject reads, be out of range, or not expose battery.
            return None
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass


class BatteryTrayApp:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._refresh_now = threading.Event()

        self._battery_service = BluetoothBatteryService()
        self._notifications = NotificationManager()

        self._devices: List[DeviceBattery] = []
        self._last_error: Optional[str] = None

        self._icon = pystray.Icon(
            "BluetoothBatteryTray",
            self._build_icon_image("--"),
            "Bluetooth Battery",
            menu=pystray.Menu(self._menu_devices_header, self._menu_separator, self._menu_refresh, self._menu_exit),
        )

    # ---------- Icon + Menu ----------
    def _build_icon_image(self, label: str) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Minimal rounded rectangle battery outline
        draw.rounded_rectangle((8, 18, 54, 46), radius=6, outline=(250, 250, 250, 255), width=3)
        draw.rectangle((54, 26, 60, 38), fill=(250, 250, 250, 255))

        # Dynamic text in the center
        font = ImageFont.load_default()
        text = label[:3]
        tw, th = draw.textbbox((0, 0), text, font=font)[2:4]
        x = (64 - tw) // 2
        y = (64 - th) // 2 - 1
        draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

        return img

    def _menu_devices_header(self, icon: pystray.Icon, item: Item):
        with self._lock:
            snapshot = list(self._devices)
            error = self._last_error

        if error:
            return Item(f"Error: {error}", None, enabled=False)

        if not snapshot:
            return Item("No battery-capable BT devices", None, enabled=False)

        lines = [f"{d.name}: {d.battery_percent}%" for d in snapshot]
        return Item(" | ".join(lines)[:120], None, enabled=False)

    def _menu_separator(self, icon: pystray.Icon, item: Item):
        return Item("—", None, enabled=False)

    def _menu_refresh(self, icon: pystray.Icon, item: Item):
        return Item("Refresh", self._on_refresh)

    def _menu_exit(self, icon: pystray.Icon, item: Item):
        return Item("Exit", self._on_exit)

    # ---------- Actions ----------
    def _on_refresh(self, icon: pystray.Icon, item: Item) -> None:
        self._refresh_now.set()

    def _on_exit(self, icon: pystray.Icon, item: Item) -> None:
        self._stop_event.set()
        icon.stop()

    # ---------- Polling ----------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                devices = asyncio.run(self._battery_service.get_connected_battery_devices())

                with self._lock:
                    self._devices = devices
                    self._last_error = None

                for d in devices:
                    self._notifications.maybe_notify(d)

                icon_label = self._calculate_icon_label(devices)
                self._icon.icon = self._build_icon_image(icon_label)
                self._icon.title = self._build_tooltip(devices)
                self._icon.update_menu()

                interval = FAST_POLL_SECONDS if devices else SLOW_POLL_SECONDS
            except Exception as exc:
                logging.exception("Polling failed")
                with self._lock:
                    self._last_error = str(exc)
                self._icon.update_menu()
                interval = SLOW_POLL_SECONDS

            # Efficient wait with early wake on manual refresh.
            if self._refresh_now.wait(timeout=interval):
                self._refresh_now.clear()

    @staticmethod
    def _calculate_icon_label(devices: List[DeviceBattery]) -> str:
        if not devices:
            return "--"
        avg = round(sum(d.battery_percent for d in devices) / len(devices))
        return str(avg)

    @staticmethod
    def _build_tooltip(devices: List[DeviceBattery]) -> str:
        if not devices:
            return "Bluetooth Battery: no battery-capable devices"
        parts = [f"{d.name} {d.battery_percent}%" for d in devices]
        return "Bluetooth Battery | " + "; ".join(parts)

    def run(self) -> None:
        worker = threading.Thread(target=self._poll_loop, name="battery-poller", daemon=True)
        worker.start()
        self._icon.run()
        self._stop_event.set()
        worker.join(timeout=5)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main() -> int:
    configure_logging()

    if sys.platform != "win32":
        logging.error("This app is intended for Windows only.")
        return 1

    app = BatteryTrayApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
