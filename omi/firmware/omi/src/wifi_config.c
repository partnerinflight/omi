#include "lib/core/wifi_config.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

bool wifi_hostname_valid(const char *host, size_t len)
{
    if (!host || !len || len > WIFI_UPLOAD_HOST_MAX)
        return false;
    size_t label = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = host[i];
        if (c == '.') {
            if (!label || host[i - 1] == '-')
                return false;
            label = 0;
        } else {
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || (c == '-' && label)))
                return false;
            if (++label > 63)
                return false;
        }
    }
    return label && host[len - 1] != '-';
}

bool wifi_config_valid(const struct wifi_upload_config *c)
{
    if (c->version != WIFI_UPLOAD_CONFIG_VERSION || !c->ssid_len || c->ssid_len > WIFI_UPLOAD_SSID_MAX ||
        c->psk_len > WIFI_UPLOAD_PSK_MAX || (c->psk_len && c->psk_len < 8) || !c->port ||
        !wifi_hostname_valid(c->hostname, strnlen(c->hostname, sizeof(c->hostname))))
        return false;
    uint8_t acc = 0;
    for (size_t i = 0; i < sizeof(c->secret); i++)
        acc |= c->secret[i];
    return acc != 0;
}

int wifi_config_parse(struct wifi_upload_config *cfg, const uint8_t *buf, size_t len)
{
    struct wifi_upload_config c = *cfg;
    bool forget = false;
    size_t i = 0;

    while (i + 2 <= len) {
        uint8_t t = buf[i];
        uint8_t l = buf[i + 1];
        i += 2;
        if (i + l > len) {
            return -EINVAL;
        }
        const uint8_t *v = buf + i;
        i += l;
        switch (t) {
        case WIFI_UPLOAD_TLV_SSID:
            if (l == 0 || l > WIFI_UPLOAD_SSID_MAX) {
                return -EINVAL;
            }
            memset(c.ssid, 0, sizeof(c.ssid));
            memcpy(c.ssid, v, l);
            c.ssid_len = l;
            break;
        case WIFI_UPLOAD_TLV_PSK:
            if (l > WIFI_UPLOAD_PSK_MAX || (l != 0 && l < 8)) {
                return -EINVAL;
            }
            memset(c.psk, 0, sizeof(c.psk));
            memcpy(c.psk, v, l);
            c.psk_len = l;
            break;
        case WIFI_UPLOAD_TLV_HOST:
            if (l != 4) {
                return -EINVAL;
            }
            memcpy(c.host, v, 4);
            snprintf(c.hostname, sizeof(c.hostname), "%u.%u.%u.%u", v[0], v[1], v[2], v[3]);
            break;
        case WIFI_UPLOAD_TLV_HOSTNAME:
            if (!wifi_hostname_valid((const char *) v, l))
                return -EINVAL;
            memset(c.hostname, 0, sizeof(c.hostname));
            memcpy(c.hostname, v, l);
            break;
        case WIFI_UPLOAD_TLV_PORT:
            if (l != 2) {
                return -EINVAL;
            }
            c.port = ((uint16_t) v[0] << 8) | v[1];
            break;
        case WIFI_UPLOAD_TLV_SECRET:
            if (l != WIFI_UPLOAD_SECRET_LEN) {
                return -EINVAL;
            }
            memcpy(c.secret, v, l);
            break;
        case WIFI_UPLOAD_TLV_ENABLE:
            if (l != 1 || v[0] > 1) {
                return -EINVAL;
            }
            c.enabled = v[0] ? 1 : 0;
            break;
        case WIFI_UPLOAD_TLV_FORGET:
            if (l != 0 || len != 2)
                return -EINVAL;
            forget = true;
            break;
        default:
            return -EINVAL;
        }
    }
    if (i != len) {
        return -EINVAL;
    }
    if (forget) {
        memset(&c, 0, sizeof(c));
    }
    c.version = WIFI_UPLOAD_CONFIG_VERSION;

    *cfg = c;
    return 0;
}
