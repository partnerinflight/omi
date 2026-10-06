#ifndef OMI_BUTTON_HOLD_H
#define OMI_BUTTON_HOLD_H
#include <stdbool.h>
#include <stdint.h>

/*
 * Every gesture is hold-then-release. While held, the motor marks each window as it is
 * reached (1 pulse: pause/resume, 2: power off, 3: Wi-Fi setup); releasing inside a window
 * commits it. Releasing early or in a dead band (5-10 s, 15-20 s) does nothing.
 */
enum button_hold_action { HOLD_NONE, HOLD_PAUSE_TOGGLE, HOLD_POWER_OFF, HOLD_SETUP };

#define BUTTON_PAUSE_MS 3000U
#define BUTTON_PAUSE_END_MS 5000U
#define BUTTON_POWER_OFF_MS 10000U
#define BUTTON_POWER_OFF_END_MS 15000U
#define BUTTON_SETUP_MS 20000U

static inline enum button_hold_action button_hold_action(uint32_t ms, bool wifi)
{
    if (ms >= BUTTON_PAUSE_MS && ms < BUTTON_PAUSE_END_MS)
        return HOLD_PAUSE_TOGGLE;
    if (ms >= BUTTON_POWER_OFF_MS && ms < BUTTON_POWER_OFF_END_MS)
        return HOLD_POWER_OFF;
    if (wifi && ms >= BUTTON_SETUP_MS)
        return HOLD_SETUP;
    return HOLD_NONE;
}

/* Vibration pulses for the window reached after holding `ms`; play each level once. */
static inline uint8_t button_hold_level(uint32_t ms, bool wifi)
{
    if (wifi && ms >= BUTTON_SETUP_MS)
        return 3;
    if (ms >= BUTTON_POWER_OFF_MS)
        return 2;
    if (ms >= BUTTON_PAUSE_MS)
        return 1;
    return 0;
}
#endif
