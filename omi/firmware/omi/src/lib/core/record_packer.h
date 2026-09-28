#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Reserved zero-frame payload. Older readers treat it as padding; updated
 * readers close the current file without creating an empty recording. */
#define RECORD_END_MAGIC "\0OMIEND\1"
#define RECORD_END_MAGIC_SIZE 8

static inline bool
record_packer_end(uint8_t *record, size_t capacity, size_t *used, bool (*emit)(const uint8_t *, size_t))
{
    if (capacity < RECORD_END_MAGIC_SIZE || *used > capacity)
        return false;
    if (*used) {
        memset(record + *used, 0, capacity - *used);
        if (!emit(record, capacity))
            return false;
        *used = 0;
    }
    memset(record, 0, capacity);
    memcpy(record, RECORD_END_MAGIC, RECORD_END_MAGIC_SIZE);
    return emit(record, capacity);
}

/* Ring payloads contain whole [length:u8][Opus frame] entries, followed by
 * zero padding. Never write the length of a frame that belongs to the next
 * record: readers would interpret stale tail bytes as audio. */
static inline bool record_packer_append(uint8_t *record,
                                        size_t capacity,
                                        size_t *used,
                                        const uint8_t *frame,
                                        size_t length,
                                        void (*emit)(const uint8_t *, size_t))
{
    if (!length || length > UINT8_MAX || length + 1 > capacity || *used > capacity)
        return false;
    if (*used + length + 1 > capacity) {
        memset(record + *used, 0, capacity - *used);
        emit(record, capacity);
        *used = 0;
    }
    record[(*used)++] = (uint8_t) length;
    memcpy(record + *used, frame, length);
    *used += length;
    if (*used == capacity) {
        emit(record, capacity);
        *used = 0;
    }
    return true;
}
