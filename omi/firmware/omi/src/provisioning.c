/*
 * Copyright (c) 2024 Nordic Semiconductor ASA
 * SPDX-License-Identifier: LicenseRef-Nordic-5-Clause
 *
 * Omi adaptation of NCS v2.9.0 softap_wifi_provision.c. The Nordic nanopb
 * /prov/networks and /prov/configure wire messages are unchanged. See
 * PROVISIONING.md for provenance and the bounded lifecycle/HTTP adaptations.
 */
#include <stdio.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/dhcpv4_server.h>
#include <zephyr/net/http/parser.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/wifi_mgmt.h>

#include "lib/core/wifi_config.h"
#include "lib/core/wifi_upload.h"
#include "pb_decode.h"
#include "pb_encode.h"
#include "portal_page.h"
#include "proto/common.pb.h"

LOG_MODULE_REGISTER(omi_provision, LOG_LEVEL_INF);
static ScanResults scan = ScanResults_init_zero;
static uint8_t scan_result_buffer[512];
static size_t scan_result_buffer_len;
static struct net_mgmt_event_callback events;
static K_SEM_DEFINE(scan_done, 0, 1);
static K_SEM_DEFINE(ap_done, 0, 1);
static int ap_result;
static uint8_t destination[300];
static size_t destination_len;
static bool saved;
static struct http_req {
    struct http_parser parser;
    bool received_all;
    enum http_method method;
    char url[32];
    size_t url_len;
    uint8_t body[512];
    size_t body_len;
} request;
static void wifi_scan_result_handle(struct net_mgmt_event_callback *cb)
{
    const struct wifi_scan_result *entry = (const struct wifi_scan_result *) cb->info;

    if (scan.results_count < ARRAY_SIZE(scan.results)) {

        scan.results[scan.results_count].has_wifi = true;

        /* SSID */
        size_t ssid_len = MIN(entry->ssid_length, sizeof(scan.results[scan.results_count].wifi.ssid.bytes));
        memcpy(scan.results[scan.results_count].wifi.ssid.bytes, entry->ssid, ssid_len);
        scan.results[scan.results_count].wifi.ssid.size = ssid_len;

        /* BSSID */
        size_t bssid_len = MIN(entry->mac_length, sizeof(scan.results[scan.results_count].wifi.bssid.bytes));
        memcpy(scan.results[scan.results_count].wifi.bssid.bytes, entry->mac, bssid_len);
        scan.results[scan.results_count].wifi.bssid.size = bssid_len;

        /* Band */
        scan.results[scan.results_count].wifi.has_band = true;
        scan.results[scan.results_count].wifi.band = (entry->band == WIFI_FREQ_BAND_2_4_GHZ) ? Band_BAND_2_4_GHZ
                                                     : (entry->band == WIFI_FREQ_BAND_5_GHZ) ? Band_BAND_5_GHZ
                                                                                             : Band_BAND_ANY;

        /* Channel */
        scan.results[scan.results_count].wifi.channel = entry->channel;

        /* Auth mode - defaults to AuthMode_WPA_WPA2_PSK. */
        scan.results[scan.results_count].wifi.has_auth = true;
        scan.results[scan.results_count].wifi.auth =
            (entry->security == WIFI_SECURITY_TYPE_NONE)         ? AuthMode_OPEN
            : (entry->security == WIFI_SECURITY_TYPE_PSK)        ? AuthMode_WPA_WPA2_PSK
            : (entry->security == WIFI_SECURITY_TYPE_PSK_SHA256) ? AuthMode_WPA2_PSK
            : (entry->security == WIFI_SECURITY_TYPE_SAE)        ? AuthMode_WPA3_PSK
                                                                 : AuthMode_WPA_WPA2_PSK;

        /* Signal strength */
        scan.results[scan.results_count].has_rssi = true;
        scan.results[scan.results_count].rssi = entry->rssi;

        scan.results_count++;
    }
}

static void event_handler(struct net_mgmt_event_callback *cb, uint32_t event, struct net_if *iface)
{
    if (event == NET_EVENT_WIFI_SCAN_RESULT)
        wifi_scan_result_handle(cb);
    else if (event == NET_EVENT_WIFI_SCAN_DONE)
        k_sem_give(&scan_done);
    else if (event == NET_EVENT_WIFI_AP_ENABLE_RESULT || event == NET_EVENT_WIFI_AP_DISABLE_RESULT) {
        const struct wifi_status *st = cb->info;
        ap_result = st ? st->status : -EIO;
        k_sem_give(&ap_done);
    }
}
static int on_body(struct http_parser *parser, const char *at, size_t length)
{
    struct http_req *req = CONTAINER_OF(parser, struct http_req, parser);

    if (req->body_len + length > sizeof(req->body)) {
        LOG_ERR("Body too large");
        return -ENOMEM;
    }

    memcpy(req->body + req->body_len, at, length);
    req->body_len += length;

    LOG_DBG("on_body: %d", parser->method);
    LOG_DBG("on_body length: %d", req->body_len);

    return 0;
}
static int on_headers_complete(struct http_parser *parser)
{
    struct http_req *req = CONTAINER_OF(parser, struct http_req, parser);

    req->method = parser->method;

    LOG_DBG("on_headers_complete, method: %s", http_method_str(parser->method));

    return 0;
}
static int on_message_complete(struct http_parser *parser)
{
    struct http_req *req = CONTAINER_OF(parser, struct http_req, parser);

    req->received_all = true;

    LOG_DBG("on_message_complete, method: %d", parser->method);

    return 0;
}

static int on_url(struct http_parser *parser, const char *at, size_t len)
{
    struct http_req *r = CONTAINER_OF(parser, struct http_req, parser);
    if (len >= sizeof(r->url) - r->url_len)
        return -ENOMEM;
    memcpy(r->url + r->url_len, at, len);
    r->url_len += len;
    r->url[r->url_len] = 0;
    return 0;
}
static int send_all(int socket, const void *buf, size_t len)
{
    const uint8_t *p = buf;
    while (len) {
        int n = zsock_send(socket, p, len, 0);
        if (n <= 0)
            return -EIO;
        p += n;
        len -= n;
    }
    return 0;
}
static int respond(int sock, int code, const char *type, const void *body, size_t len)
{
    char header[256];
    int n = snprintf(header,
                     sizeof(header),
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %u\r\n"
                     "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n",
                     code,
                     code == 200 ? "OK" : "Bad Request",
                     type,
                     (unsigned) len);
    if (n < 0 || n >= sizeof(header))
        return -EINVAL;
    int err = send_all(sock, header, n);
    return err ? err : send_all(sock, body, len);
}
static int configure(void)
{
    WifiConfig creds = WifiConfig_init_zero;
    pb_istream_t stream = pb_istream_from_buffer(request.body, request.body_len);
    if (!destination_len || !pb_decode(&stream, WifiConfig_fields, &creds) || !creds.has_wifi ||
        !creds.wifi.ssid.size || (creds.wifi.auth != AuthMode_OPEN && creds.wifi.auth != AuthMode_WPA_WPA2_PSK))
        return -EINVAL;
    if ((creds.wifi.auth == AuthMode_OPEN && creds.passphrase.size) ||
        (creds.wifi.auth != AuthMode_OPEN && creds.passphrase.size < 8))
        return -EINVAL;
    return wifi_upload_provision(destination,
                                 destination_len,
                                 creds.wifi.ssid.bytes,
                                 creds.wifi.ssid.size,
                                 creds.passphrase.bytes,
                                 creds.passphrase.size);
}
static int handle(int sock)
{
    const char *msg = "Invalid configuration. Check all fields and retry.";
    if (!strcmp(request.url, "/") && request.method == HTTP_GET)
        return respond(sock, 200, "text/html; charset=utf-8", portal_page, sizeof(portal_page) - 1);
    if (!strcmp(request.url, "/prov/networks") && request.method == HTTP_GET)
        return respond(sock, 200, "application/x-protobuf", scan_result_buffer, scan_result_buffer_len);
    if (!strcmp(request.url, "/omi/destination") && request.method == HTTP_POST) {
        /* Only destination fields are accepted on this extension to Nordic's API. */
        size_t i = 0;
        unsigned seen = 0;
        while (i + 2 <= request.body_len) {
            uint8_t type = request.body[i], len = request.body[i + 1];
            unsigned bit = type == WIFI_UPLOAD_TLV_HOSTNAME ? 1
                           : type == WIFI_UPLOAD_TLV_PORT   ? 2
                           : type == WIFI_UPLOAD_TLV_SECRET ? 4
                                                            : 0;
            if (!bit || (seen & bit) || i + 2 + len > request.body_len)
                break;
            seen |= bit;
            i += 2 + len;
        }
        struct wifi_upload_config c = {0};
        if (i == request.body_len && seen == 7 && request.body_len <= sizeof(destination) &&
            wifi_config_parse(&c, request.body, request.body_len) == 0 && c.port) {
            uint8_t key = 0;
            for (size_t j = 0; j < sizeof(c.secret); j++)
                key |= c.secret[j];
            if (key) {
                memcpy(destination, request.body, request.body_len);
                destination_len = request.body_len;
                return respond(sock, 200, "text/plain", "Ready", 5);
            }
        }
        destination_len = 0;
    } else if (!strcmp(request.url, "/prov/configure") && request.method == HTTP_POST) {
        if (configure() == 0) {
            saved = true;
            msg = "Saved. Setup closes now; Omi will connect and test the receiver. Check BLE wifi-status for the "
                  "result.";
            return respond(sock, 200, "text/plain", msg, strlen(msg));
        }
    }
    return respond(sock, 400, "text/plain", msg, strlen(msg));
}
static void process_client(int server, int64_t portal_deadline)
{
    int sock = zsock_accept(server, NULL, NULL);
    if (sock < 0)
        return;
    struct zsock_timeval tv = {.tv_sec = 2};
    zsock_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    zsock_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    memset(&request, 0, sizeof(request));
    struct http_parser_settings parser_settings;
    http_parser_settings_init(&parser_settings);
    parser_settings.on_body = on_body;
    parser_settings.on_headers_complete = on_headers_complete;
    parser_settings.on_message_complete = on_message_complete;
    parser_settings.on_url = on_url;
    http_parser_init(&request.parser, HTTP_REQUEST);
    int64_t deadline = MIN(portal_deadline, k_uptime_get() + 5000);
    size_t total = 0;
    char buf[256];
    while (k_uptime_get() < deadline && total < 2048) {
        int n = zsock_recv(sock, buf, sizeof(buf), 0);
        if (n <= 0)
            break;
        total += n;
        size_t parsed = http_parser_execute(&request.parser, &parser_settings, buf, n);
        if (HTTP_PARSER_ERRNO(&request.parser) != HPE_OK || parsed != n)
            break;
        if (request.received_all) {
            handle(sock);
            break;
        }
    }
    zsock_close(sock);
    memset(request.body, 0, sizeof(request.body));
}

/* Called only by the Wi-Fi owner thread; recording/BLE keep running. */
int omi_provision_run(void)
{
    struct net_if *iface = net_if_get_first_wifi();
    if (!iface)
        return -ENODEV;
    int ret = net_if_up(iface), server = -1;
    bool ap_requested = false, dhcp_started = false;
    struct in_addr address, netmask, pool;
    zsock_inet_pton(AF_INET, "192.168.4.1", &address);
    zsock_inet_pton(AF_INET, "255.255.255.0", &netmask);
    zsock_inet_pton(AF_INET, "192.168.4.2", &pool);
    if (ret && ret != -EALREADY)
        return ret;
    saved = false;
    destination_len = 0;
    memset(&scan, 0, sizeof(scan));
    net_mgmt_init_event_callback(&events,
                                 event_handler,
                                 NET_EVENT_WIFI_SCAN_RESULT | NET_EVENT_WIFI_SCAN_DONE |
                                     NET_EVENT_WIFI_AP_ENABLE_RESULT | NET_EVENT_WIFI_AP_DISABLE_RESULT);
    net_mgmt_add_event_callback(&events);
    k_sem_reset(&scan_done);
    struct wifi_scan_params scan_params = {0};
    ret = net_mgmt(NET_REQUEST_WIFI_SCAN, iface, &scan_params, sizeof(scan_params));
    if (!ret)
        k_sem_take(&scan_done, K_SECONDS(10));
    pb_ostream_t stream = pb_ostream_from_buffer(scan_result_buffer, sizeof(scan_result_buffer));
    scan_result_buffer_len = pb_encode(&stream, ScanResults_fields, &scan) ? stream.bytes_written : 0;
    struct net_linkaddr *mac = net_if_get_link_addr(iface);
    char ssid[24];
    snprintf(ssid, sizeof(ssid), "OMI-Setup-%02X%02X", mac->addr[4], mac->addr[5]);
    struct wifi_connect_req_params params = {
        .ssid = (uint8_t *) ssid,
        .ssid_length = strlen(ssid),
        .channel = 6,
        .band = WIFI_FREQ_BAND_2_4_GHZ,
        .security = WIFI_SECURITY_TYPE_NONE,
        .mfp = WIFI_MFP_OPTIONAL,
        .timeout = SYS_FOREVER_MS,
    };
    k_sem_reset(&ap_done);
    ret = net_mgmt(NET_REQUEST_WIFI_AP_ENABLE, iface, &params, sizeof(params));
    if (ret)
        goto out;
    ap_requested = true;
    if (k_sem_take(&ap_done, K_SECONDS(15)) || ap_result) {
        ret = -EIO;
        goto out;
    }
    if (!net_if_ipv4_addr_add(iface, &address, NET_ADDR_MANUAL, 0)) {
        ret = -ENOMEM;
        goto out;
    }
    net_if_ipv4_set_netmask_by_addr(iface, &address, &netmask);
    ret = net_dhcpv4_server_start(iface, &pool);
    if (ret)
        goto out;
    dhcp_started = true;
    server = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (server < 0) {
        ret = -errno;
        goto out;
    }
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(80), .sin_addr = address};
    int reuse = 1;
    zsock_setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    if (zsock_bind(server, (struct sockaddr *) &addr, sizeof(addr)) || zsock_listen(server, 1)) {
        ret = -errno;
        goto out;
    }
    LOG_INF("Setup: %s, http://192.168.4.1 (5 minute window)", ssid);
    int64_t deadline = k_uptime_get() + 300000;
    while (!saved && k_uptime_get() < deadline) {
        struct zsock_pollfd fd = {.fd = server, .events = ZSOCK_POLLIN};
        int ready = zsock_poll(&fd, 1, 250);
        if (ready < 0) {
            ret = -errno;
            goto out;
        }
        if (ready > 0 && (fd.revents & ZSOCK_POLLIN))
            process_client(server, deadline);
    }
    ret = saved ? 0 : -ETIMEDOUT;
    if (saved)
        k_sleep(K_MSEC(500));
out:
    if (server >= 0)
        zsock_close(server);
    if (dhcp_started)
        net_dhcpv4_server_stop(iface);
    if (ap_requested) {
        k_sem_reset(&ap_done);
        if (!net_mgmt(NET_REQUEST_WIFI_AP_DISABLE, iface, NULL, 0))
            k_sem_take(&ap_done, K_SECONDS(5));
    }
    net_if_ipv4_addr_rm(iface, &address);
    net_mgmt_del_event_callback(&events);
    net_if_down(iface);
    memset(destination, 0, sizeof(destination));
    return ret;
}
