#ifndef DOCK_H
#define DOCK_H

#include <stdbool.h>

/* Dock power. CV1 mainboard v1.2 divides the dock's VIN (R39/R41 over R38/R40, about
 * 0.6 x VIN) onto VIN_DETECT, P1.06. Unlike the charger's CHG output it stays high when a
 * docked battery is full. Current flowing into the battery also proves dock power, which
 * covers a VIN_DETECT level that reads under the logic threshold. */
static inline bool dock_present_from(bool vin_detect, bool charging)
{
    return vin_detect || charging;
}

/* Reads the level once (no debounce at boot) and tells the microphone owner, so call it
 * after battery_init() and before mic_start(). */
int dock_init(void);
/* Debounced dock power. */
bool dock_power_present(void);
/* Raw VIN_DETECT level, for diagnostics. */
bool dock_vin_detect_raw(void);
/* ISR-safe: an edge on VIN_DETECT or CHG restarts the debounce. */
void dock_recheck(void);
/* Periodic backstop for a missed edge; never restarts a pending debounce. */
void dock_poll(void);

#endif
