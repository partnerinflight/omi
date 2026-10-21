#ifndef OMI_BUTTON_TAPS_H
#define OMI_BUTTON_TAPS_H
#include <stdbool.h>
#include <stdint.h>

/*
 * Short-press sequences. A tap is a press released within BUTTON_TAP_MAX_MS; the next tap
 * must start at most BUTTON_TAP_GAP_MS after the previous release. A sequence is reported
 * once, when that window closes, so a triple-click never reports a double tap first. A
 * fourth tap cancels the sequence; a longer press (a hold) ends it without a result, and
 * button_hold.h decides what the hold does.
 */
enum button_tap_result { TAPS_NONE = 0, TAPS_SINGLE = 1, TAPS_DOUBLE = 2, TAPS_TRIPLE = 3 };

#define BUTTON_TAP_MAX_MS 300U
#define BUTTON_TAP_GAP_MS 600U
#define BUTTON_TAPS_CANCELLED 0xFFU

struct button_taps {
    bool pressed;
    uint8_t count; /* taps in the open sequence, or BUTTON_TAPS_CANCELLED */
    uint32_t press_ms;
    uint32_t release_ms;
};

static inline enum button_tap_result button_taps_close(struct button_taps *t)
{
    enum button_tap_result done = t->count == BUTTON_TAPS_CANCELLED ? TAPS_NONE : (enum button_tap_result) t->count;
    t->count = 0;
    return done;
}

/* Feed every button poll. Returns a sequence's result once, when its window has closed. */
static inline enum button_tap_result button_taps_update(struct button_taps *t, uint32_t now, bool pressed)
{
    enum button_tap_result done = TAPS_NONE;

    if (pressed && !t->pressed) {
        if (t->count && now - t->release_ms > BUTTON_TAP_GAP_MS)
            done = button_taps_close(t); /* this press starts a new sequence */
        t->press_ms = now;
    } else if (!pressed && t->pressed) {
        if (now - t->press_ms < BUTTON_TAP_MAX_MS) {
            if (t->count != BUTTON_TAPS_CANCELLED)
                t->count = t->count == 3 ? BUTTON_TAPS_CANCELLED : (uint8_t) (t->count + 1);
            t->release_ms = now;
        } else {
            t->count = 0; /* a hold cancels the tap count */
        }
    } else if (!pressed && t->count && now - t->release_ms > BUTTON_TAP_GAP_MS) {
        done = button_taps_close(t);
    }
    t->pressed = pressed;
    return done;
}

static inline bool button_taps_pending(const struct button_taps *t)
{
    return t->count != 0;
}
#endif
