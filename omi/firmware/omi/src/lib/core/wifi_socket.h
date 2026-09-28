#ifndef OMI_WIFI_SOCKET_H
#define OMI_WIFI_SOCKET_H
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>

#define WIFI_SOCKET_WRITE_BYTES 1024U
#define WIFI_SOCKET_PROGRESS_TIMEOUT_MS 15000

/* Bound individual allocations below the TCP send window and wait for
 * queued bytes to release buffers when the network applies backpressure. */
static inline int wifi_socket_send_all(int sock, const uint8_t *data, size_t length)
{
    int64_t deadline = k_uptime_get() + WIFI_SOCKET_PROGRESS_TIMEOUT_MS;
    while (length) {
        size_t chunk = length < WIFI_SOCKET_WRITE_BYTES ? length : WIFI_SOCKET_WRITE_BYTES;
        ssize_t sent = zsock_send(sock, data, chunk, 0);
        if (sent > 0) {
            data += sent;
            length -= (size_t) sent;
            deadline = k_uptime_get() + WIFI_SOCKET_PROGRESS_TIMEOUT_MS;
            continue;
        }
        if (sent == 0)
            return -ECONNRESET;
        int error = errno;
        if (error != EAGAIN && error != ENOBUFS && error != ENOMEM && error != EINTR)
            return -error;
        if (k_uptime_get() >= deadline)
            return -ETIMEDOUT;
        k_sleep(K_MSEC(25));
    }
    return 0;
}
#endif
