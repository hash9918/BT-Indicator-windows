# Bluetooth Battery Tray (Windows 10/11)

A production-ready Python tray app that monitors BLE Bluetooth battery levels and shows a dynamic tray icon.

## Features
- System tray icon with live battery text (average % of discovered battery-capable devices)
- BLE battery polling via GATT Battery Service (`0x180F`) and Battery Level Characteristic (`0x2A19`)
- Low battery notifications when a device drops below **20%** (only once per discharge cycle)
- Tray menu actions: device list, **Refresh**, **Exit**
- Low CPU design using timed waits and background polling thread

## Install
```powershell
py -3.11 -m pip install -r requirements.txt
```

## Run
```powershell
py bluetooth_battery_tray.py
```

## Build a single EXE with PyInstaller

1. Install build dependency:
   ```powershell
   py -3.11 -m pip install pyinstaller
   ```
2. Build:
   ```powershell
   py -3.11 -m PyInstaller --noconfirm --onefile --windowed --name BluetoothBatteryTray bluetooth_battery_tray.py
   ```
3. Output EXE:
   - `dist\BluetoothBatteryTray.exe`

## Run at startup (current user)

Option A (Startup folder):
1. Press `Win + R`, run: `shell:startup`
2. Copy `BluetoothBatteryTray.exe` into that folder.

Option B (Task Scheduler, recommended):
1. Open **Task Scheduler** → **Create Task**
2. Trigger: **At log on**
3. Action: **Start a program** → select `BluetoothBatteryTray.exe`
4. Enable **Run only when user is logged on** for tray icon visibility.

## Notes
- Devices must expose BLE battery GATT characteristics. Classic Bluetooth devices without battery GATT are not readable through bleak.
- Notifications reset once a device rises to 25% or above.
