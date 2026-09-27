#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A per-device local unicast address, independent of missing nRF7002 OTP.
 * Fold all hardware-ID bytes into six octets; never write the chip's OTP. */
static inline bool wifi_mac_from_id(const uint8_t *id, size_t length, uint8_t mac[6])
{
    if (length < 6)
        return false;
    for (size_t i = 0; i < 6; ++i)
        mac[i] = id[i];
    for (size_t i = 6; i < length; ++i)
        mac[i % 6] ^= id[i];
    mac[0] = (mac[0] & 0xfc) | 0x02;
    return true;
}
