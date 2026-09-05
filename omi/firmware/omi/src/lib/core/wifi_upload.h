#ifndef WIFI_UPLOAD_H
#define WIFI_UPLOAD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/toolchain.h>

/*
 * Wi-Fi upload of recordings to ONE pre-provisioned receiver on the local
 * network (omi-local serve), triggered while the device is on the charger.
 *
 * - Credentials + receiver are provisioned over BLE (storage.c config char)
 *   and stored in the settings subsystem ("omi/wifi_upload").
 * - The nRF7002 is powered only for the duration of an upload session.
 * - The receiver must prove knowledge of the shared secret before any audio
 *   is sent; the device proves itself too (mutual HMAC-SHA256 handshake).
 * - Records are deleted from the ring ONLY after the receiver acknowledges
 *   that it persisted them (the user opted into delete-after-verified-upload).
 */

#define WIFI_UPLOAD_CONFIG_VERSION 1
#define WIFI_UPLOAD_SSID_MAX 32
#define WIFI_UPLOAD_PSK_MAX 64
#define WIFI_UPLOAD_SECRET_LEN 32

struct wifi_upload_config {
    uint8_t version;
    uint8_t enabled;
    uint8_t ssid_len;
    uint8_t psk_len;
    uint8_t ssid[WIFI_UPLOAD_SSID_MAX];
    uint8_t psk[WIFI_UPLOAD_PSK_MAX];
    uint8_t host[4]; /* receiver IPv4, network byte order */
    uint16_t port;   /* receiver TCP port, host byte order */
    uint8_t secret[WIFI_UPLOAD_SECRET_LEN];
} __packed;

/* Status as exposed on the BLE config characteristic (little-endian). */
struct wifi_upload_status {
    uint8_t configured;
    uint8_t state;       /* enum wifi_upload_state */
    uint8_t last_result; /* enum wifi_upload_result */
    uint8_t reserved;
    int32_t last_errno;
    uint32_t sessions_ok;
    uint32_t packets_uploaded;
    uint32_t last_attempt_uptime_s;
    uint32_t heap_free;
    uint32_t heap_max_used;
} __packed;

enum wifi_upload_state {
    WIFI_UPLOAD_IDLE = 0,
    WIFI_UPLOAD_WAIT_SD,
    WIFI_UPLOAD_WIFI_UP,
    WIFI_UPLOAD_CONNECTING,
    WIFI_UPLOAD_DHCP,
    WIFI_UPLOAD_TCP,
    WIFI_UPLOAD_AUTH,
    WIFI_UPLOAD_UPLOADING,
    WIFI_UPLOAD_TEARDOWN,
};

enum wifi_upload_result {
    WIFI_UPLOAD_OK = 0,
    WIFI_UPLOAD_ERR_NOT_CONFIGURED,
    WIFI_UPLOAD_ERR_SD_NOT_READY,
    WIFI_UPLOAD_ERR_WIFI_CONNECT,
    WIFI_UPLOAD_ERR_DHCP,
    WIFI_UPLOAD_ERR_TCP_CONNECT,
    WIFI_UPLOAD_ERR_AUTH,
    WIFI_UPLOAD_ERR_PROTOCOL,
    WIFI_UPLOAD_ERR_RING_READ,
    WIFI_UPLOAD_ERR_LINK_LOST,
    WIFI_UPLOAD_ERR_ABORTED,
    WIFI_UPLOAD_ERR_BUSY,
    WIFI_UPLOAD_ERR_NOTHING_TO_DO,
};

/* TLV types accepted by wifi_upload_apply_tlv() (BLE provisioning). */
#define WIFI_UPLOAD_TLV_SSID 0x01
#define WIFI_UPLOAD_TLV_PSK 0x02
#define WIFI_UPLOAD_TLV_HOST 0x03   /* 4 bytes IPv4 */
#define WIFI_UPLOAD_TLV_PORT 0x04   /* u16 big-endian */
#define WIFI_UPLOAD_TLV_SECRET 0x05 /* 32 bytes */
#define WIFI_UPLOAD_TLV_ENABLE 0x06 /* u8 0/1 */
#define WIFI_UPLOAD_TLV_FORGET 0x7F /* erase the configuration */

#ifdef CONFIG_OMI_WIFI_UPLOAD

int wifi_upload_init(void);

/** Parse a TLV blob written over BLE and persist the resulting config. */
int wifi_upload_apply_tlv(const uint8_t *buf, size_t len);

/** Ask for an upload session now (ignores the charger condition). */
int wifi_upload_request_now(void);

void wifi_upload_get_status(struct wifi_upload_status *out);

/** True while a session holds the SD / storage bulk buffer. */
bool wifi_upload_active(void);

#else

static inline bool wifi_upload_active(void)
{
    return false;
}

#endif /* CONFIG_OMI_WIFI_UPLOAD */

#endif /* WIFI_UPLOAD_H */
