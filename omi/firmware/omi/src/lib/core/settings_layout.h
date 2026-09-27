#ifndef OMI_SETTINGS_LAYOUT_H
#define OMI_SETTINGS_LAYOUT_H
/* Recovered 42a433de2 CV1 build: persistent NVS ABI, shared by BLE/Wi-Fi builds. */
_Static_assert(PM_SETTINGS_STORAGE_ADDRESS == 0xf8000, "CV1 settings must retain the legacy NVS address");
_Static_assert(PM_SETTINGS_STORAGE_SIZE == 0x2000, "CV1 settings must retain the legacy NVS size");
#endif
