# PortalSync for developers

How PortalSync works, how to build it and how to test it. To install and use it, see the [README](../README.md).

## How it works

- **The virtual portal:** `portalsync.py` is a USB/IP server. usbip-win2 attaches its virtual portal, and Windows sees a second `1430:0150` HID device. The portal protocol (status reports, block reads and writes, lights, audio) is ported from Dolphin's emulated portal.
- **The real portal:** PortalSync claims it with libusb, reads each figure placed on it, and loads it into a free virtual slot. Game writes go to `dumps\<character> (<id>) - current.sky`, and to the real figure when write-back is on.
- **Hiding the real portal:** `portal_hide.dll` is loaded into each running Cemu, RPCS3, Dolphin and shadPS4 process. It patches `SetupDiGetDeviceInstanceIdA` so that it fails for a portal behind a real (PCI) USB controller while the virtual portal exists, and libusb then skips the real portal. Nothing is written to the emulator folders, and the patch goes away when the emulator closes.
- **Reading figure saves:** saves are decrypted the way Dolphin does it (AES from Windows itself, no extra package). Checksum types 1–3 cover the main save area, and type 6 covers the second area that holds the later games' experience.
- **Portal quirks found while testing:** this portal sometimes answers with the previous block's data, sends repeated status reports, and lets audio block control transfers. PortalSync handles all three.

## Building from source

To run it from the source instead of the exe (steps 1, 2 and 4 of [Getting started](../README.md#getting-started) still apply):

```
pip install -r requirements.txt
python portalsync.py
```

Python 3.13 was used. To build the exe and the DLL together, install PyInstaller as well, then run `powershell -ExecutionPolicy Bypass -File build.ps1`. This needs the Visual Studio C++ x64 build tools. To build only `portal_hide.dll`, use an *x64 Native Tools Command Prompt for VS 2022*:

```
cl /nologo /O2 /LD portal_hide.c cfgmgr32.lib
```

## Tests

```
python test_portalsync.py
python -m unittest -v test_save_safety
```

The first prints `ok`, and the second runs 35 save-safety tests. The tests use fake portals and a loopback USB/IP client, so no hardware is needed.

## Files

| File | What it is |
| --- | --- |
| `portalsync.py` | The program: USB/IP server, portal logic, window. |
| `portal_hide.c` | Source of `portal_hide.dll`. |
| `portal_descriptors.json` | USB descriptors of a real portal, which the virtual portal copies. Recreate them from your own portal with `python portalsync.py capture`. |
| `figure_names.json` | Figure IDs and names: Dolphin's Skylander list, the Imaginators Senseis from Cemu's and RPCS3's lists, and the Imaginators variants and Creation Crystals from [Skylander-IDs](https://github.com/Texthead1/Skylander-IDs). |
| `figure_details.json` | Game and element of each figure in the All Skylanders list. |
| `test_portalsync.py`, `test_save_safety.py` | Tests. |
| `build.ps1` | Builds `PortalSync.exe` and `portal_hide.dll`. |
| `portal.ico` | Icon. |

The figure format is in [FIGURE_FORMAT.md](FIGURE_FORMAT.md).
