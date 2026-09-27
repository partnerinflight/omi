#ifndef OMI_WIFI_LIMITS_H
#define OMI_WIFI_LIMITS_H
/* NCS 2.9 hostap uses packet/control/event sockets; Zephyr's socket Kconfig
 * defaults to six poll entries with WPA supplicant. Four caused select() to
 * return ENOMEM and terminate the real CV1's supplicant during startup. */
_Static_assert(CONFIG_NET_SOCKETS_POLL_MAX >= 6, "Nordic supplicant needs at least six socket poll entries");
/* Real CV1 startup fault: mgmt_work_q_obj exhausted the SDK's 4200 bytes
 * while adding the supplicant interface. Keep room for its control calls. */
_Static_assert(CONFIG_NET_MGMT_EVENT_STACK_SIZE >= 8192, "CV1 supplicant startup needs an 8 KiB management stack");
#endif
