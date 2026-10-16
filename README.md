# Skylanders Portal Bridge

Use one real Skylanders Portal of Power with **Cemu** (Wii U), **RPCS3** (PS3) and **Dolphin** (Wii) on Windows.

Portal Bridge creates a *virtual* portal over USB/IP. Every figure you place on the real portal is copied into the virtual portal, and the emulator talks only to the virtual one. The real portal can stay plugged in the whole time: a small DLL hides it from the emulators, so games never say "too many Portals".

![The Portal Bridge window](docs/window.png)

## Features

- **Works with Cemu, RPCS3 and Dolphin**, with the real portal plugged in.
- **Game saves go back to your figures** ("Write saves to real figures", on by default). Only data blocks are written, and every block is read back to check it.
- **Backups of every figure** in the `dumps` folder, named after the character, for example `Zook (86bc1263) - original.sky`. Each figure keeps one backup, its newest. An older backup goes to the Recycle Bin, never straight to deletion.
- **Restore saved progress.** If a figure's saved copy has progress the figure lacks, a pop-up offers to write it to the figure, one figure at a time. What the figure held before is kept as its backup, so a restore can be undone.
- **Figure data.** Each figure's save is decrypted to show its level, gold and nickname in the Add Skylander list and the restore pop-up. For a Trap Team trap, it shows the villain inside (a Villain column in the Add Skylander list, for example Air Jughead: Bad Juju).
- **Damaged saves are caught.** When a figure's newest save fails its checksum, the game says "A toy on the portal has a problem". Portal Bridge warns you as soon as such a figure is placed and offers its last intact backup. A damaged copy is never offered, written to a figure, or kept in place of an intact backup.
- **Reset figure.** Select a figure from the real portal and click **Reset figure…** to erase its progress, like a toy fresh out of the box. What it held is kept as its backup, so **Restore progress…** can undo it.
- **Drag and drop** `.sky` files onto the window to load them.
- **Update check.** At start Portal Bridge looks up the latest release on GitHub, and shows a link in the window when there is a newer version.
- **Trap Team sound** plays on the Traptanium Portal's own speaker, or on the PC speakers when no Traptanium Portal is connected.
- **Portal lights** follow the game.
- **Unplugging is safe.** Figures on the virtual portal stay loaded while the real portal is away, and saves made meanwhile are written when it comes back.
- **Add Skylander window** with a search box, no file explorer needed:
  - **Your Skylanders** lists every figure you have played. Load its current copy or its backup (a backup is loaded as a copy, so the backup itself never changes).
  - **All Skylanders** lists all 548 figures, the 31 Imaginators Senseis included. **Create new figure** makes a blank one, fresh out of the box, the way Dolphin and RPCS3 create figures. It works without a real portal.
  - **Browse files…** still loads any `.sky` file from elsewhere.

![The Add Skylander window](docs/add.png)

![The restore pop-up](docs/restore.png)

## Tested

| Emulator | Game | Notes |
| --- | --- | --- |
| Cemu 2.6 | Skylanders Trap Team, SuperChargers, Swap Force | Traps, vehicles, Swap Force halves, portal speaker. |
| RPCS3 | Skylanders Giants (BLES01689) | |
| Dolphin | Skylanders Spyro's Adventure | Needs USB passthrough, see below. |

All of them were tested with a Traptanium Portal (USB ID `1430:0150`) on Windows 11.

## Requirements

- Windows 10 or 11, 64-bit.
- [usbip-win2](https://github.com/vadimgrn/usbip-win2), tested with 0.9.8.1, installed in `C:\Program Files\USBip`.
- The real portal on a libusb-compatible driver. Install **libusbK** (or WinUSB) for `Spyro Porta` (`1430:0150`) with [Zadig](https://zadig.akeo.ie/).

## Install

### The exe (easiest)

1. Download the zip from the [latest release](../../releases/latest) and unzip it to a folder of its own. The folder holds `PortalBridge.exe`, `portal_hide.dll`, `portal_descriptors.json` and `figure_names.json`; keep them together.
2. Double-click `PortalBridge.exe`. Your figure dumps go to a `dumps` folder next to it.

Some antivirus programs distrust exes packed with PyInstaller, and programs that load a DLL into another process. Both are what Portal Bridge does, so it may be flagged. You can check the source here and build both files yourself.

### From source

```
pip install -r requirements.txt
python portal_bridge.py
```

Python 3.13 was used. Build `portal_hide.dll` in an *x64 Native Tools Command Prompt for VS 2022*:

```
cl /nologo /O2 /LD portal_hide.c cfgmgr32.lib
```

## Usage

1. Plug in the real portal.
2. Start Portal Bridge **before** the emulator boots a game.
3. Start the emulator and the game. The Activity log shows that the real portal is hidden from it.
4. Place figures on the real portal. Each one appears in the game.

Close the window to stop. That disconnects the virtual portal.

If a game was already running when you started the bridge, the game holds the real portal. The bridge then shows a pop-up: stop the game, unplug and replug the real portal, and start the game again.

### Emulator settings

- **Cemu:** no settings needed. In the Cemu hybrid build, keep the *Hybrid* checkbox off.
- **RPCS3:** start the bridge before booting the game. With no portal at boot, RPCS3 adds its own emulated portal.
- **Dolphin:** add `1430:0150` to the USB passthrough whitelist, and keep Dolphin's own emulated Skylanders portal off.

## How it works

- `portal_bridge.py` is a USB/IP server. usbip-win2 attaches its virtual portal, and Windows sees a second `1430:0150` HID device. The portal protocol (status reports, block reads and writes, lights, audio) is ported from Dolphin's emulated portal.
- The bridge claims the real portal with libusb, reads each figure placed on it, and loads it into a free virtual slot. Game writes go to `dumps\<character> (<id>) - current.sky`, and to the real figure when write-back is on.
- `portal_hide.dll` is loaded into each running Cemu, RPCS3 and Dolphin process. It patches `SetupDiGetDeviceInstanceIdA` so that it fails for a portal behind a real (PCI) USB controller while the virtual portal exists. libusb then skips the real portal. Nothing is written to the emulator folders, and the patch goes away when the emulator closes.
- The real portal's quirks found while testing are handled: stale block reads (the bridge checks every read), repeated status reports, and audio blocking control transfers.

## Tests

```
python test_portal_bridge.py
```

It prints `ok`. The tests use fake portals and a loopback USB/IP client, so no hardware is needed.

## Files

| File | What it is |
| --- | --- |
| `portal_bridge.py` | The program: USB/IP server, portal logic, window. |
| `portal_hide.c` | Source of `portal_hide.dll`. |
| `portal_descriptors.json` | USB descriptors of a real portal, which the virtual portal copies. Recreate them from your own portal with `python portal_bridge.py capture`. |
| `figure_names.json` | Figure IDs and names (548 figures): Dolphin's Skylander list plus the 31 Imaginators Senseis from Cemu's and RPCS3's lists. Creation Crystals are not in it; neither emulator lists them. |
| `test_portal_bridge.py` | Tests. |
| `portal.ico` | Icon. |

Your figure dumps, the log and the window settings stay local (`dumps\`, `bridge.log`, `settings.json`). They are never part of the repository.

## Credits

- The portal protocol and the figure list come from [Dolphin](https://github.com/dolphin-emu/dolphin) (`Source/Core/Core/IOS/USB/Emulated/Skylanders/`), cross-checked with [RPCS3](https://github.com/RPCS3/rpcs3) and [Cemu](https://github.com/cemu-project/Cemu).
- [usbip-win2](https://github.com/vadimgrn/usbip-win2) provides the virtual USB controller.

## License

GPL-2.0-or-later, the same as Dolphin, whose portal code this project is ported from. See [LICENSE](LICENSE).

Skylanders and Portal of Power are trademarks of Activision. This project is not affiliated with or endorsed by Activision.
