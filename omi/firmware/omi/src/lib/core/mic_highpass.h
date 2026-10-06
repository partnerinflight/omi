#ifndef OMI_MIC_HIGHPASS_H
#define OMI_MIC_HIGHPASS_H

#include <stddef.h>
#include <stdint.h>

/*
 * Recorded-audio rumble filter (mic.c process_audio_buffer): a 4th-order Butterworth
 * high-pass at 100 Hz on 16 kHz mono PCM, applied in place before VOX and the codec.
 * Car road/wind boom sits mostly below 60 Hz (-18 dB at 60 Hz, -42 dB at 30 Hz);
 * speech above 150 Hz is untouched (-0.2 dB). Output saturates to int16.
 */
struct mic_highpass {
    float z[2][2]; /* two transposed direct form II sections */
};

void mic_highpass_reset(struct mic_highpass *f);
void mic_highpass_apply(struct mic_highpass *f, int16_t *pcm, size_t frames);

#endif
