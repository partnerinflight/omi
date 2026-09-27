#ifndef OMI_BUTTON_HOLD_H
#define OMI_BUTTON_HOLD_H
#include <stdbool.h>
#include <stdint.h>
enum button_hold_action { HOLD_NONE, HOLD_POWER_OFF, HOLD_SETUP };
static inline enum button_hold_action button_hold_action(uint32_t ms, bool released, bool wifi)
{
    if (wifi) {
        if (!released && ms >= 5000)
            return HOLD_SETUP;
        if (released && ms >= 3000 && ms < 5000)
            return HOLD_POWER_OFF;
    } else if (!released && ms >= 3000)
        return HOLD_POWER_OFF;
    return HOLD_NONE;
}
#endif
