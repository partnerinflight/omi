#ifndef OMI_BATTERY_FILTER_H
#define OMI_BATTERY_FILTER_H
#include <stdbool.h>
#include <stdint.h>

/* Keep the fractional remainder: rounding each sample to whole percent
 * previously held a reported 1% forever for raw values up to 4%. */
struct battery_filter {
    uint32_t q8;
    bool initialized;
};
static inline uint8_t battery_filter_update(struct battery_filter *f, uint8_t raw, bool charging)
{
    if (raw > 100)
        raw = 100;
    uint32_t target = (uint32_t) raw * 256;
    if (!f->initialized) {
        f->q8 = target;
        f->initialized = true;
    } else {
        if (charging && target < f->q8)
            target = f->q8;
        if (!charging && target > f->q8)
            target = f->q8;
        int32_t delta = (int32_t) target - (int32_t) f->q8;
        int32_t step = delta / 6;
        if (!step && delta)
            step = delta > 0 ? 1 : -1;
        f->q8 = (uint32_t) ((int32_t) f->q8 + step);
    }
    return (uint8_t) ((f->q8 + 128) / 256);
}
#endif
