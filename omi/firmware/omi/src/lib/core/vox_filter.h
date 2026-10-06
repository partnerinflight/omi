#ifndef OMI_VOX_FILTER_H
#define OMI_VOX_FILTER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Speech-band level for the silence timer (mic.c aad_track_silence). Each 100 ms block
 * of 16 kHz mono PCM passes a 250 Hz high-pass then a 3.4 kHz low-pass (Butterworth
 * biquads), so keystrokes and rumble -- loud mostly below 200 Hz -- barely register.
 * A block only counts as sound when at least `sustain` of the last `window` blocks were
 * at or above the threshold, so a single click cannot reset the timer.
 */
struct vox_biquad {
    float z1, z2;
};

struct vox_filter {
    struct vox_biquad hp, lp;
    uint8_t history; /* bit n set: the block n updates ago was loud */
};

void vox_filter_reset(struct vox_filter *f);
uint32_t vox_block_level(struct vox_filter *f, const int16_t *pcm, size_t frames);
bool vox_voice_detected(struct vox_filter *f, uint32_t level, uint32_t threshold, unsigned sustain, unsigned window);

#endif
