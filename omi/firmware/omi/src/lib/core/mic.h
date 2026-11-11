#ifndef MIC_H
#define MIC_H

#include <stdbool.h>
#include <stdint.h>

typedef void (*mix_handler)(int16_t *);

/**
 * @brief Initialize the Microphone
 *
 * Initializes the Microphone
 *
 * @return 0 if successful, negative errno code if error
 */
int mic_start();
void set_mic_callback(mix_handler _callback);

void mic_off();
void mic_on();
int mic_pause(void);
int mic_resume(void);
bool mic_is_running();
void mic_set_gain(uint8_t gain_level);

/* True while the mic is in hardware AAD sleep (mic off, waiting for sound).
 * Lets other subsystems (e.g. the status LED) drop to their lowest-power state. */
bool mic_in_aad_sleep(void);
/* Nonblocking request; the microphone owner performs power/codec transitions. */
int mic_toggle_manual_pause(void);
/* Dock power changed (dock.c); the owner stops the mic while docked. */
void mic_set_docked(bool docked);
/* Mic off for any pause (manual or docked). */
bool mic_is_paused(void);
/* User-requested pause only, for the red pause pulse. */
bool mic_is_manually_paused(void);
bool mic_manual_pause_led_on(void);
/* Nonblocking triple-click toggle; the microphone owner writes the markers. */
int mic_toggle_lecture(void);
bool mic_in_lecture(void);
#endif
