/*
 * Wi-Fi upload of recordings to one pre-provisioned local receiver.
 * See lib/core/wifi_upload.h for the design summary and the protocol below.
 *
 * Wire protocol (TCP, all integers big-endian, each message is
 * [type:u8][len:u32][payload]):
 *   C->S HELLO     0x01  "OMIL" ver:u8=1 device_id:6 client_nonce:16
 *   S->C CHALLENGE 0x02  server_nonce:16 server_tag:32
 *   C->S AUTH      0x03  client_tag:32 read:u64 write:u64 cap:u32 dropped:u64 pkt:u16 codec:u8
 *   S->C START     0x04  start_seq:u64          (or REJECT 0x7F reason:u8)
 *   C->S DATA      0x05  seq:u64 count:u16 records[count*444]
 *   S->C ACK       0x06  next_seq:u64           (receiver has PERSISTED < next_seq)
 *   C->S DONE      0x07  next_seq:u64
 *   S->C BYE       0x08  next_seq:u64
 * server_tag = HMAC(secret, "omi-local-srv"||cn||sn), client_tag likewise with
 * "omi-local-cli". The device only sends audio after verifying server_tag, and
 * only advances (deletes) the ring up to a seq the receiver has ACKed.
 */

#include "lib/core/wifi_upload.h"

#include <errno.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/dhcpv4.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/wifi_mgmt.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/byteorder.h>
#ifdef CONFIG_SYS_HEAP_RUNTIME_STATS
#include <zephyr/sys/sys_heap.h>
#endif

#include "lib/core/config.h"
#include "lib/core/local_auth.h"
#include "lib/core/mic.h"
#include "lib/core/sd_card.h"
#include "lib/core/settings.h"
#include "lib/core/storage.h"

LOG_MODULE_REGISTER(wifi_upload, CONFIG_LOG_DEFAULT_LEVEL);

extern bool is_charging;  /* main.c */
extern bool is_connected; /* main.c */

#define MSG_HELLO 0x01
#define MSG_CHALLENGE 0x02
#define MSG_AUTH 0x03
#define MSG_START 0x04
#define MSG_DATA 0x05
#define MSG_ACK 0x06
#define MSG_DONE 0x07
#define MSG_BYE 0x08
#define MSG_REJECT 0x7F

#define UP_PROTOCOL_VERSION 1
#define UP_HDR_LEN 5
#define UP_MAX_CTRL_PAYLOAD 64
#define UP_DEVICE_ID_LEN 6

#define UP_ADVANCE_EVERY_PACKETS (4U * 36U) /* persist the delete every ~64 KiB */
#define UP_MAX_PASSES 4
#define UP_MIN_UNREAD_PACKETS 600U /* ~1 minute of audio before bothering */
#define UP_RETRY_INTERVAL_MS (5 * 60 * 1000)
#define UP_POLL_INTERVAL_S 10
#define UP_SD_READY_TIMEOUT_MS 6000
#define UP_WIFI_CONNECT_TIMEOUT_MS 30000
#define UP_DHCP_TIMEOUT_MS 25000
#define UP_SOCKET_TIMEOUT_S 20

K_THREAD_STACK_DEFINE(wifi_upload_stack, 6144);
static struct k_thread wifi_upload_thread;
static K_SEM_DEFINE(trigger_sem, 0, 1);
static K_SEM_DEFINE(connect_sem, 0, 1);
static K_SEM_DEFINE(ip_sem, 0, 1);
static atomic_t manual_req = ATOMIC_INIT(0);
static atomic_t active = ATOMIC_INIT(0);
static atomic_t link_lost = ATOMIC_INIT(0);
static int connect_status;
static int64_t last_attempt_ms;

static struct wifi_upload_config cfg;
static struct wifi_upload_status status;
static struct net_mgmt_event_callback wifi_mgmt_cb;
static struct net_mgmt_event_callback ipv4_mgmt_cb;

static void set_state(enum wifi_upload_state s)
{
    status.state = (uint8_t) s;
}

static bool cfg_valid(void)
{
    if (cfg.version != WIFI_UPLOAD_CONFIG_VERSION || !cfg.enabled) {
        return false;
    }
    if (cfg.ssid_len == 0 || cfg.ssid_len > WIFI_UPLOAD_SSID_MAX || cfg.psk_len > WIFI_UPLOAD_PSK_MAX) {
        return false;
    }
    if (cfg.port == 0 || (cfg.host[0] | cfg.host[1] | cfg.host[2] | cfg.host[3]) == 0) {
        return false;
    }
    uint8_t acc = 0;
    for (size_t i = 0; i < sizeof(cfg.secret); i++) {
        acc |= cfg.secret[i];
    }
    return acc != 0;
}

/* --- net_mgmt events ------------------------------------------------------ */

static void wifi_mgmt_event_handler(struct net_mgmt_event_callback *cb, uint32_t mgmt_event, struct net_if *iface)
{
    ARG_UNUSED(iface);
    if (mgmt_event == NET_EVENT_WIFI_CONNECT_RESULT) {
        const struct wifi_status *st = cb->info;
        connect_status = st ? st->status : -1;
        k_sem_give(&connect_sem);
    } else if (mgmt_event == NET_EVENT_WIFI_DISCONNECT_RESULT) {
        atomic_set(&link_lost, 1);
    }
}

static void ipv4_mgmt_event_handler(struct net_mgmt_event_callback *cb, uint32_t mgmt_event, struct net_if *iface)
{
    ARG_UNUSED(cb);
    ARG_UNUSED(iface);
    if (mgmt_event == NET_EVENT_IPV4_ADDR_ADD) {
        k_sem_give(&ip_sem);
    }
}

/* --- socket helpers -------------------------------------------------------- */

static int send_all(int sock, const uint8_t *p, size_t n)
{
    while (n > 0) {
        ssize_t w = zsock_send(sock, p, n, 0);
        if (w <= 0) {
            return -EIO;
        }
        p += w;
        n -= (size_t) w;
    }
    return 0;
}

static int recv_all(int sock, uint8_t *p, size_t n)
{
    while (n > 0) {
        ssize_t r = zsock_recv(sock, p, n, 0);
        if (r <= 0) {
            return -EIO;
        }
        p += r;
        n -= (size_t) r;
    }
    return 0;
}

static int send_frame(int sock, uint8_t type, const uint8_t *payload, uint32_t len)
{
    uint8_t hdr[UP_HDR_LEN];
    hdr[0] = type;
    sys_put_be32(len, hdr + 1);
    int ret = send_all(sock, hdr, sizeof(hdr));
    if (ret == 0 && len > 0) {
        ret = send_all(sock, payload, len);
    }
    return ret;
}

/* Receive one control frame (payload bounded by UP_MAX_CTRL_PAYLOAD). */
static int recv_ctrl_frame(int sock, uint8_t *type, uint8_t *payload, uint32_t *len)
{
    uint8_t hdr[UP_HDR_LEN];
    int ret = recv_all(sock, hdr, sizeof(hdr));
    if (ret) {
        return ret;
    }
    *type = hdr[0];
    *len = sys_get_be32(hdr + 1);
    if (*len > UP_MAX_CTRL_PAYLOAD) {
        return -EMSGSIZE;
    }
    return recv_all(sock, payload, *len);
}

static int recv_expect_u64(int sock, uint8_t want_type, uint64_t *value, uint8_t *got_type)
{
    uint8_t payload[UP_MAX_CTRL_PAYLOAD];
    uint32_t len;
    int ret = recv_ctrl_frame(sock, got_type, payload, &len);
    if (ret) {
        return ret;
    }
    if (*got_type != want_type) {
        return -EPROTO;
    }
    if (len != 8) {
        return -EPROTO;
    }
    *value = sys_get_be64(payload);
    return 0;
}

/* --- the session ----------------------------------------------------------- */

static void device_id_get(uint8_t out[UP_DEVICE_ID_LEN])
{
    bt_addr_le_t addrs[CONFIG_BT_ID_MAX];
    size_t count = CONFIG_BT_ID_MAX;
    memset(out, 0, UP_DEVICE_ID_LEN);
    bt_id_get(addrs, &count);
    if (count > 0) {
        memcpy(out, addrs[0].a.val, UP_DEVICE_ID_LEN);
    }
}

static enum wifi_upload_result handshake(int sock, const sd_ring_info_t *info, uint64_t *start_seq, int *err)
{
    uint8_t cn[LOCAL_AUTH_NONCE_LEN];
    uint8_t sn[LOCAL_AUTH_NONCE_LEN];
    uint8_t tag[LOCAL_AUTH_HASH_LEN];
    uint8_t payload[UP_MAX_CTRL_PAYLOAD];
    uint32_t len;
    uint8_t type;

    if (sys_csrand_get(cn, sizeof(cn)) != 0) {
        sys_rand_get(cn, sizeof(cn));
    }

    uint8_t hello[4 + 1 + UP_DEVICE_ID_LEN + LOCAL_AUTH_NONCE_LEN];
    memcpy(hello, "OMIL", 4);
    hello[4] = UP_PROTOCOL_VERSION;
    device_id_get(hello + 5);
    memcpy(hello + 5 + UP_DEVICE_ID_LEN, cn, sizeof(cn));
    *err = send_frame(sock, MSG_HELLO, hello, sizeof(hello));
    if (*err) {
        return WIFI_UPLOAD_ERR_LINK_LOST;
    }

    *err = recv_ctrl_frame(sock, &type, payload, &len);
    if (*err) {
        return WIFI_UPLOAD_ERR_LINK_LOST;
    }
    if (type != MSG_CHALLENGE || len != LOCAL_AUTH_NONCE_LEN + LOCAL_AUTH_HASH_LEN) {
        *err = -EPROTO;
        return WIFI_UPLOAD_ERR_PROTOCOL;
    }
    memcpy(sn, payload, sizeof(sn));
    local_auth_tag(cfg.secret, "omi-local-srv", cn, sn, tag);
    if (!local_auth_ct_equal(tag, payload + LOCAL_AUTH_NONCE_LEN, LOCAL_AUTH_HASH_LEN)) {
        LOG_ERR("receiver failed authentication; not sending audio");
        *err = -EACCES;
        return WIFI_UPLOAD_ERR_AUTH;
    }

    /* AUTH: our proof + ring info */
    uint8_t auth[LOCAL_AUTH_HASH_LEN + 31];
    local_auth_tag(cfg.secret, "omi-local-cli", cn, sn, auth);
    uint8_t *p = auth + LOCAL_AUTH_HASH_LEN;
    sys_put_be64(info->read_seq, p);
    sys_put_be64(info->write_seq, p + 8);
    sys_put_be32(info->capacity_packets, p + 16);
    sys_put_be64(info->dropped_packets, p + 20);
    sys_put_be16(RAW_AUDIO_PACKET_BYTES, p + 28);
    p[30] = CODEC_ID;
    *err = send_frame(sock, MSG_AUTH, auth, sizeof(auth));
    if (*err) {
        return WIFI_UPLOAD_ERR_LINK_LOST;
    }

    *err = recv_ctrl_frame(sock, &type, payload, &len);
    if (*err) {
        return WIFI_UPLOAD_ERR_LINK_LOST;
    }
    if (type == MSG_REJECT) {
        LOG_ERR("receiver rejected session (reason %u)", len ? payload[0] : 0U);
        *err = -EACCES;
        return (len && payload[0] == 1) ? WIFI_UPLOAD_ERR_AUTH : WIFI_UPLOAD_ERR_PROTOCOL;
    }
    if (type != MSG_START || len != 8) {
        *err = -EPROTO;
        return WIFI_UPLOAD_ERR_PROTOCOL;
    }
    *start_seq = sys_get_be64(payload);
    if (*start_seq < info->read_seq || *start_seq > info->write_seq) {
        *err = -ERANGE;
        return WIFI_UPLOAD_ERR_PROTOCOL;
    }
    return WIFI_UPLOAD_OK;
}

static enum wifi_upload_result upload_records(int sock, bool manual, sd_ring_info_t *info, uint64_t seq, int *err)
{
    size_t buf_len = 0;
    uint8_t *buf = storage_shared_bulk_buffer(&buf_len);
    uint32_t chunk_packets = (uint32_t) (buf_len / RAW_AUDIO_PACKET_BYTES);
    uint64_t last_advanced = seq;
    uint64_t end = info->write_seq;
    uint8_t got_type;

    for (int pass = 0; pass < UP_MAX_PASSES; pass++) {
        while (seq < end) {
            if (!manual && !is_charging) {
                *err = -ECANCELED;
                return WIFI_UPLOAD_ERR_ABORTED;
            }
            if (atomic_get(&link_lost)) {
                *err = -ENOTCONN;
                return WIFI_UPLOAD_ERR_LINK_LOST;
            }

            uint32_t want = (uint32_t) MIN((uint64_t) chunk_packets, end - seq);
            uint32_t bytes_read = 0;
            uint32_t packets_read = 0;
            int ret = sd_ring_read(seq, buf, want * RAW_AUDIO_PACKET_BYTES, &bytes_read, &packets_read);
            if (ret < 0) {
                *err = ret;
                return WIFI_UPLOAD_ERR_RING_READ;
            }
            if (packets_read == 0U) {
                break;
            }

            uint8_t prefix[10];
            sys_put_be64(seq, prefix);
            sys_put_be16((uint16_t) packets_read, prefix + 8);
            uint8_t hdr[UP_HDR_LEN];
            hdr[0] = MSG_DATA;
            sys_put_be32(sizeof(prefix) + bytes_read, hdr + 1);
            if (send_all(sock, hdr, sizeof(hdr)) || send_all(sock, prefix, sizeof(prefix)) ||
                send_all(sock, buf, bytes_read)) {
                *err = -EIO;
                return WIFI_UPLOAD_ERR_LINK_LOST;
            }

            uint64_t next_seq = 0;
            ret = recv_expect_u64(sock, MSG_ACK, &next_seq, &got_type);
            if (ret) {
                *err = ret;
                return (ret == -EPROTO) ? WIFI_UPLOAD_ERR_PROTOCOL : WIFI_UPLOAD_ERR_LINK_LOST;
            }
            if (next_seq != seq + packets_read) {
                LOG_ERR("receiver acked %llu, expected %llu",
                        (unsigned long long) next_seq,
                        (unsigned long long) (seq + packets_read));
                *err = -EPROTO;
                return WIFI_UPLOAD_ERR_PROTOCOL;
            }

            seq = next_seq;
            status.packets_uploaded += packets_read;

            /* Delete-after-verified-upload: the receiver persisted < seq. */
            if (seq - last_advanced >= UP_ADVANCE_EVERY_PACKETS) {
                if (sd_ring_advance(seq) == 0) {
                    last_advanced = seq;
                }
            }
        }

        if (seq > last_advanced && sd_ring_advance(seq) == 0) {
            last_advanced = seq;
        }

        /* Audio kept arriving during the pass: catch up if it is worth a chunk. */
        if (sd_ring_get_info(info) != 0 || info->write_seq - seq < chunk_packets) {
            break;
        }
        end = info->write_seq;
    }

    uint8_t done[8];
    sys_put_be64(seq, done);
    if (send_frame(sock, MSG_DONE, done, sizeof(done)) == 0) {
        uint64_t bye = 0;
        (void) recv_expect_u64(sock, MSG_BYE, &bye, &got_type); /* best effort */
    }
    *err = 0;
    return WIFI_UPLOAD_OK;
}

static enum wifi_upload_result run_session(bool manual, int *err)
{
    enum wifi_upload_result result;
    struct net_if *iface = NULL;
    int sock = -1;
    bool wifi_up = false;
    sd_ring_info_t info;

    *err = 0;
    set_state(WIFI_UPLOAD_WAIT_SD);
    sd_request_power(true);
    int64_t deadline = k_uptime_get() + UP_SD_READY_TIMEOUT_MS;
    while (!sd_is_ready() && k_uptime_get() < deadline) {
        k_msleep(100);
    }
    if (!sd_is_ready()) {
        result = WIFI_UPLOAD_ERR_SD_NOT_READY;
        goto out;
    }
    if (storage_transfer_active()) {
        result = WIFI_UPLOAD_ERR_BUSY;
        goto out;
    }
    storage_set_upload_busy(true);
    atomic_set(&active, 1);

    *err = sd_ring_get_info(&info);
    if (*err) {
        result = WIFI_UPLOAD_ERR_RING_READ;
        goto out;
    }
    if (info.write_seq == info.read_seq) {
        result = WIFI_UPLOAD_ERR_NOTHING_TO_DO;
        goto out;
    }

    set_state(WIFI_UPLOAD_WIFI_UP);
    iface = net_if_get_first_wifi();
    if (!iface) {
        *err = -ENODEV;
        result = WIFI_UPLOAD_ERR_WIFI_CONNECT;
        goto out;
    }
    atomic_clear(&link_lost);
    k_sem_reset(&connect_sem);
    k_sem_reset(&ip_sem);
    *err = net_if_up(iface);
    if (*err && *err != -EALREADY) {
        result = WIFI_UPLOAD_ERR_WIFI_CONNECT;
        goto out;
    }
    wifi_up = true;

    set_state(WIFI_UPLOAD_CONNECTING);
    struct wifi_connect_req_params params = {
        .ssid = cfg.ssid,
        .ssid_length = cfg.ssid_len,
        .psk = cfg.psk_len ? cfg.psk : NULL,
        .psk_length = cfg.psk_len,
        .security = cfg.psk_len ? WIFI_SECURITY_TYPE_PSK : WIFI_SECURITY_TYPE_NONE,
        .mfp = WIFI_MFP_OPTIONAL,
        .band = WIFI_FREQ_BAND_UNKNOWN,
        .channel = WIFI_CHANNEL_ANY,
        .timeout = SYS_FOREVER_MS,
    };
    *err = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface, &params, sizeof(params));
    if (*err) {
        result = WIFI_UPLOAD_ERR_WIFI_CONNECT;
        goto out;
    }
    if (k_sem_take(&connect_sem, K_MSEC(UP_WIFI_CONNECT_TIMEOUT_MS)) != 0 || connect_status != 0) {
        *err = connect_status ? connect_status : -ETIMEDOUT;
        result = WIFI_UPLOAD_ERR_WIFI_CONNECT;
        goto out;
    }

    set_state(WIFI_UPLOAD_DHCP);
    net_dhcpv4_start(iface);
    if (k_sem_take(&ip_sem, K_MSEC(UP_DHCP_TIMEOUT_MS)) != 0) {
        *err = -ETIMEDOUT;
        result = WIFI_UPLOAD_ERR_DHCP;
        goto out;
    }

    set_state(WIFI_UPLOAD_TCP);
    sock = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) {
        *err = -errno;
        result = WIFI_UPLOAD_ERR_TCP_CONNECT;
        goto out;
    }
    struct zsock_timeval tv = {.tv_sec = UP_SOCKET_TIMEOUT_S, .tv_usec = 0};
    (void) zsock_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    (void) zsock_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons(cfg.port)};
    memcpy(&sa.sin_addr, cfg.host, sizeof(cfg.host));
    if (zsock_connect(sock, (struct sockaddr *) &sa, sizeof(sa)) < 0) {
        *err = -errno;
        result = WIFI_UPLOAD_ERR_TCP_CONNECT;
        goto out;
    }

    set_state(WIFI_UPLOAD_AUTH);
    uint64_t start_seq = info.read_seq;
    result = handshake(sock, &info, &start_seq, err);
    if (result != WIFI_UPLOAD_OK) {
        goto out;
    }

    set_state(WIFI_UPLOAD_UPLOADING);
    result = upload_records(sock, manual, &info, start_seq, err);

out:
    set_state(WIFI_UPLOAD_TEARDOWN);
    if (sock >= 0) {
        (void) zsock_close(sock);
    }
    if (wifi_up) {
        net_dhcpv4_stop(iface);
        (void) net_mgmt(NET_REQUEST_WIFI_DISCONNECT, iface, NULL, 0);
        (void) net_if_down(iface); /* powers the nRF7002 down */
    }
    storage_set_upload_busy(false);
    atomic_clear(&active);
    if (mic_in_aad_sleep() && !is_connected) {
        sd_request_power(false);
    }
    set_state(WIFI_UPLOAD_IDLE);
    return result;
}

static void upload_thread_fn(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);

    while (1) {
        k_sem_take(&trigger_sem, K_SECONDS(UP_POLL_INTERVAL_S));
        bool manual = atomic_cas(&manual_req, 1, 0);

        if (!cfg_valid()) {
            if (manual) {
                status.last_result = WIFI_UPLOAD_ERR_NOT_CONFIGURED;
            }
            continue;
        }
        if (!manual) {
            if (!is_charging) {
                continue;
            }
            int64_t now = k_uptime_get();
            if (last_attempt_ms != 0 && (now - last_attempt_ms) < UP_RETRY_INTERVAL_MS) {
                continue;
            }
            if (sd_ring_peek_unread() < UP_MIN_UNREAD_PACKETS) {
                continue;
            }
        }

        last_attempt_ms = k_uptime_get();
        status.last_attempt_uptime_s = (uint32_t) (last_attempt_ms / 1000);
        int err = 0;
        enum wifi_upload_result r = run_session(manual, &err);
        status.last_result = (uint8_t) r;
        status.last_errno = err;
        if (r == WIFI_UPLOAD_OK) {
            status.sessions_ok++;
            LOG_INF("upload session ok (total %u packets)", status.packets_uploaded);
        } else {
            LOG_WRN("upload session failed: result %d err %d", r, err);
        }
    }
}

/* --- public API --------------------------------------------------------------- */

int wifi_upload_init(void)
{
    (void) app_settings_get_wifi_upload(&cfg);
    status.configured = cfg_valid() ? 1 : 0;

    net_mgmt_init_event_callback(
        &wifi_mgmt_cb, wifi_mgmt_event_handler, NET_EVENT_WIFI_CONNECT_RESULT | NET_EVENT_WIFI_DISCONNECT_RESULT);
    net_mgmt_add_event_callback(&wifi_mgmt_cb);
    net_mgmt_init_event_callback(&ipv4_mgmt_cb, ipv4_mgmt_event_handler, NET_EVENT_IPV4_ADDR_ADD);
    net_mgmt_add_event_callback(&ipv4_mgmt_cb);

    /* The nRF70 interface is NET_IF_NO_AUTO_START; make sure it is down (RPU
     * unpowered) until a session needs it. */
    struct net_if *iface = net_if_get_first_wifi();
    if (iface && net_if_is_up(iface)) {
        (void) net_if_down(iface);
    }

    k_thread_create(&wifi_upload_thread,
                    wifi_upload_stack,
                    K_THREAD_STACK_SIZEOF(wifi_upload_stack),
                    upload_thread_fn,
                    NULL,
                    NULL,
                    NULL,
                    K_PRIO_PREEMPT(8),
                    0,
                    K_NO_WAIT);
    k_thread_name_set(&wifi_upload_thread, "wifi_upload");
    LOG_INF("Wi-Fi upload %s", status.configured ? "configured" : "not configured");
    return 0;
}

int wifi_upload_apply_tlv(const uint8_t *buf, size_t len)
{
    struct wifi_upload_config c = cfg;
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
            break;
        case WIFI_UPLOAD_TLV_PORT:
            if (l != 2) {
                return -EINVAL;
            }
            c.port = sys_get_be16(v);
            break;
        case WIFI_UPLOAD_TLV_SECRET:
            if (l != WIFI_UPLOAD_SECRET_LEN) {
                return -EINVAL;
            }
            memcpy(c.secret, v, l);
            break;
        case WIFI_UPLOAD_TLV_ENABLE:
            if (l != 1) {
                return -EINVAL;
            }
            c.enabled = v[0] ? 1 : 0;
            break;
        case WIFI_UPLOAD_TLV_FORGET:
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

    int ret = app_settings_save_wifi_upload(&c);
    if (ret) {
        return ret;
    }
    cfg = c;
    status.configured = cfg_valid() ? 1 : 0;
    LOG_INF("Wi-Fi upload config updated (%s)", status.configured ? "valid" : "incomplete/disabled");
    return 0;
}

int wifi_upload_request_now(void)
{
    if (!cfg_valid()) {
        return -ENOENT;
    }
    atomic_set(&manual_req, 1);
    k_sem_give(&trigger_sem);
    return 0;
}

void wifi_upload_get_status(struct wifi_upload_status *out)
{
    *out = status;
    out->configured = cfg_valid() ? 1 : 0;
#ifdef CONFIG_SYS_HEAP_RUNTIME_STATS
    extern struct k_heap _system_heap;
    struct sys_memory_stats st;
    if (sys_heap_runtime_stats_get(&_system_heap.heap, &st) == 0) {
        out->heap_free = (uint32_t) st.free_bytes;
        out->heap_max_used = (uint32_t) st.max_allocated_bytes;
    }
#endif
}

bool wifi_upload_active(void)
{
    return atomic_get(&active) != 0;
}
