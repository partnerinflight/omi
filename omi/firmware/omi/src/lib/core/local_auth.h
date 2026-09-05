#ifndef LOCAL_AUTH_H
#define LOCAL_AUTH_H

/*
 * Tiny, dependency-free SHA-256 / HMAC-SHA256 used to authenticate the local
 * Wi-Fi upload receiver. Plain C so it can be compiled and tested on the host
 * (see scripts/omi-local/tests/test_local_auth_c.py).
 */

#include <stddef.h>
#include <stdint.h>

#define LOCAL_AUTH_HASH_LEN 32
#define LOCAL_AUTH_NONCE_LEN 16
#define LOCAL_AUTH_SECRET_LEN 32

void local_sha256(const uint8_t *data, size_t len, uint8_t out[LOCAL_AUTH_HASH_LEN]);
void local_hmac_sha256(const uint8_t *key,
                       size_t key_len,
                       const uint8_t *msg,
                       size_t msg_len,
                       uint8_t out[LOCAL_AUTH_HASH_LEN]);

/** Constant-time comparison. Returns 1 when equal, 0 otherwise. */
int local_auth_ct_equal(const uint8_t *a, const uint8_t *b, size_t len);

/**
 * Handshake tag = HMAC-SHA256(secret, label || client_nonce || server_nonce).
 * label is "omi-local-srv" for the receiver's proof and "omi-local-cli" for the
 * device's proof, so neither side can replay the other's tag.
 */
void local_auth_tag(const uint8_t secret[LOCAL_AUTH_SECRET_LEN],
                    const char *label,
                    const uint8_t client_nonce[LOCAL_AUTH_NONCE_LEN],
                    const uint8_t server_nonce[LOCAL_AUTH_NONCE_LEN],
                    uint8_t out[LOCAL_AUTH_HASH_LEN]);

#endif /* LOCAL_AUTH_H */
