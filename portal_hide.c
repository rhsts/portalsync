/* portal_hide.dll: PortalSync loads this into Cemu, RPCS3 and Dolphin so their libusb never lists
the real portal.
Copyright (C) 2026 rhsts. SPDX-License-Identifier: GPL-2.0-or-later (see LICENSE).

Cemu counts every 1430:0150 libusb lists when a game boots; real + virtual portal gives "too many
Portals". RPCS3 and Dolphin rescan while a game runs and pass each one to the game. libusb on Windows skips a device whose instance ID it cannot read (os/windows_winusb.c,
"could not read the device instance ID ... skipping"), so this replaces SetupDiGetDeviceInstanceIdA
in the Cemu process with a version that fails for a real portal while a virtual portal is present.
The virtual portal is the one whose USB host controller is made by software (usbip-win2's vhci:
its device ID starts with ROOT\, see is_virtual). Without a virtual portal nothing
is hidden, so stopping PortalSync gives Cemu the real portal back at the next game boot.

Build (x64 Native Tools Command Prompt for VS 2022, in this folder):
  cl /nologo /O2 /LD portal_hide.c cfgmgr32.lib
*/
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <tlhelp32.h>
#include <string.h>

#ifndef _M_X64
#error portal_hide.c patches x64 code (mov rax, imm64; jmp rax): build it with the x64 compiler
#endif

static const char PORTAL[] = "USB\\VID_1430&PID_0150\\";

/* The portal's root hub sits on its host controller. usbip-win2's is ROOT\USB\0001; a real one is PCI\...
   (or ACPI\... on machines whose controller is not on PCI), both seen on the author's PC, 2026-11-11.
   "Not PCI" alone called a real portal on such a machine the virtual one, and nothing was hidden. */
static BOOL is_virtual(DEVINST d)
{
    char id[MAX_DEVICE_ID_LEN];
    while (CM_Get_Parent(&d, d, 0) == CR_SUCCESS)
        if (CM_Get_Device_IDA(d, id, sizeof id, 0) == CR_SUCCESS && _strnicmp(id, "USB\\ROOT_HUB", 12) == 0)
            return CM_Get_Parent(&d, d, 0) == CR_SUCCESS && CM_Get_Device_IDA(d, id, sizeof id, 0) == CR_SUCCESS
                && _strnicmp(id, "ROOT\\", 5) == 0;
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
            && is_virtual(d))
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
    if (_strnicmp(id, PORTAL, sizeof PORTAL - 1) == 0 && !is_virtual(data->DevInst) && virtual_portal_present()) {
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

/* Suspend (or resume) every other thread of this process listed in the snapshot. Nothing here takes the heap
   lock, which a thread suspended a moment ago may hold: that is why the snapshot is made once, before. */
static void other_threads(HANDLE snapshot, BOOL suspend)
{
    THREADENTRY32 t;
    t.dwSize = sizeof t;
    if (snapshot == INVALID_HANDLE_VALUE || !Thread32First(snapshot, &t))
        return;
    do {
        if (t.th32OwnerProcessID == GetCurrentProcessId() && t.th32ThreadID != GetCurrentThreadId()) {
            HANDLE thread = OpenThread(THREAD_SUSPEND_RESUME, FALSE, t.th32ThreadID);
            if (thread) {
                if (suspend)
                    SuspendThread(thread);
                else
                    ResumeThread(thread);
                CloseHandle(thread);
            }
        }
    } while (Thread32Next(snapshot, &t));
}

BOOL WINAPI DllMain(HINSTANCE dll, DWORD reason, LPVOID reserved)
{
    (void)dll;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        /* mov rax, get_instance_id; jmp rax. Never undone: the DLL stays loaded until the emulator exits.
           The 12-byte write is not atomic, so the other threads stand still while it is made: an emulator
           scans USB on a thread of its own, also right after it starts.
           ponytail: a thread stopped inside those very 12 bytes would still crash; if that ever shows,
           check each thread's RIP and let it run on first. */
        unsigned char jump[12] = {0x48, 0xB8, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xE0};
        HMODULE setupapi = GetModuleHandleA("setupapi.dll");  /* Cemu imports it; no LoadLibrary under the loader lock
                                                                 (PortalSync tries again later when it is not loaded yet) */
        BYTE *target = setupapi ? (BYTE *)GetProcAddress(setupapi, "SetupDiGetDeviceInstanceIdA") : NULL;
        void *hook = (void *)get_instance_id;
        HANDLE threads;
        DWORD old;
        if (!target || !VirtualProtect(target, sizeof jump, PAGE_EXECUTE_READWRITE, &old))
            return FALSE;
        memcpy(jump + 2, &hook, sizeof hook);
        threads = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        other_threads(threads, TRUE);
        memcpy(target, jump, sizeof jump);
        FlushInstructionCache(GetCurrentProcess(), target, sizeof jump);
        other_threads(threads, FALSE);
        if (threads != INVALID_HANDLE_VALUE)
            CloseHandle(threads);
        VirtualProtect(target, sizeof jump, old, &old);
    }
    return TRUE;
}
