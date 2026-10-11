/* portal_hide.dll: portal-bridge loads this into Cemu, RPCS3 and Dolphin so their libusb never lists
the real portal.
Copyright (C) 2026 rhsts. SPDX-License-Identifier: GPL-2.0-or-later (see LICENSE).

Cemu counts every 1430:0150 libusb lists when a game boots; real + virtual portal gives "too many
Portals". RPCS3 and Dolphin rescan while a game runs and pass each one to the game. libusb on Windows skips a device whose instance ID it cannot read (os/windows_winusb.c,
"could not read the device instance ID ... skipping"), so this replaces SetupDiGetDeviceInstanceIdA
in the Cemu process with a version that fails for a real portal (one behind a PCI host controller)
while a virtual portal (usbip-win2's vhci is not PCI) is present. Without a virtual portal nothing
is hidden, so stopping the bridge gives Cemu the real portal back at the next game boot.

Build (x64 Native Tools Command Prompt for VS 2022, in this folder):
  cl /nologo /O2 /LD portal_hide.c cfgmgr32.lib
*/
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <string.h>

static const char PORTAL[] = "USB\\VID_1430&PID_0150\\";

static BOOL behind_pci(DEVINST d)
{
    char id[MAX_DEVICE_ID_LEN];
    while (CM_Get_Parent(&d, d, 0) == CR_SUCCESS)
        if (CM_Get_Device_IDA(d, id, sizeof id, 0) == CR_SUCCESS && _strnicmp(id, "PCI\\", 4) == 0)
            return TRUE;
    return FALSE;
}

static BOOL virtual_portal_present(void)
{
    char list[64 * 1024];
    if (CM_Get_Device_ID_ListA("USB", list, sizeof list, CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT) != CR_SUCCESS)
        return FALSE;
    for (char *id = list; *id; id += strlen(id) + 1) {
        DEVINST d;
        if (_strnicmp(id, PORTAL, sizeof PORTAL - 1) == 0 && CM_Locate_DevNodeA(&d, id, CM_LOCATE_DEVNODE_NORMAL) == CR_SUCCESS
            && !behind_pci(d))
            return TRUE;
    }
    return FALSE;
}

/* Stands in for SetupDiGetDeviceInstanceIdA, whose result is the devnode's instance ID. */
static BOOL WINAPI get_instance_id(HDEVINFO set, PSP_DEVINFO_DATA data, PSTR buf, DWORD size, PDWORD required)
{
    char id[MAX_DEVICE_ID_LEN];
    DWORD n;
    (void)set;
    if (!data || data->cbSize != sizeof *data || CM_Get_Device_IDA(data->DevInst, id, sizeof id, 0) != CR_SUCCESS) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    if (_strnicmp(id, PORTAL, sizeof PORTAL - 1) == 0 && behind_pci(data->DevInst) && virtual_portal_present()) {
        SetLastError(ERROR_NO_SUCH_DEVINST);
        return FALSE;
    }
    n = (DWORD)strlen(id) + 1;
    if (required)
        *required = n;
    if (!buf || size < n) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }
    memcpy(buf, id, n);
    return TRUE;
}

BOOL WINAPI DllMain(HINSTANCE dll, DWORD reason, LPVOID reserved)
{
    (void)dll;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        /* mov rax, get_instance_id; jmp rax. Never undone: the DLL stays loaded until the emulator exits.
           ponytail: the 12-byte write is not atomic; a thread inside the function's first bytes right
           now could crash. The bridge injects before a game boots, when nothing enumerates USB; if it
           ever matters, suspend the other threads around the write. */
        unsigned char jump[12] = {0x48, 0xB8, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xE0};
        HMODULE setupapi = GetModuleHandleA("setupapi.dll");  /* Cemu imports it; no LoadLibrary under the loader lock */
        BYTE *target = setupapi ? (BYTE *)GetProcAddress(setupapi, "SetupDiGetDeviceInstanceIdA") : NULL;
        void *hook = (void *)get_instance_id;
        DWORD old;
        if (!target || !VirtualProtect(target, sizeof jump, PAGE_EXECUTE_READWRITE, &old))
            return FALSE;
        memcpy(jump + 2, &hook, sizeof hook);
        memcpy(target, jump, sizeof jump);
        VirtualProtect(target, sizeof jump, old, &old);
        FlushInstructionCache(GetCurrentProcess(), target, sizeof jump);
    }
    return TRUE;
}
