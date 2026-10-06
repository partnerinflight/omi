#include "mic_highpass.h"

#include <string.h>

/* RBJ cookbook high-pass sections at 16 kHz, 100 Hz, Butterworth Q = 0.5412 and 1.3066:
 * b0, b1, b2, a1, a2 (a0 normalised). */
static const float SECTIONS[2][5] = {
    {0.964626232f, -1.929252464f, 0.964626232f, -1.928508485f, 0.929996442f},
    {0.984818525f, -1.969637050f, 0.984818525f, -1.968877497f, 0.970396602f},
};

void mic_highpass_reset(struct mic_highpass *f)
{
    memset(f, 0, sizeof(*f));
}

void mic_highpass_apply(struct mic_highpass *f, int16_t *pcm, size_t frames)
{
    for (size_t i = 0; i < frames; i++) {
        float x = (float) pcm[i];
        for (int s = 0; s < 2; s++) {
            const float *c = SECTIONS[s];
            float *z = f->z[s];
            float y = c[0] * x + z[0];
            z[0] = c[1] * x - c[3] * y + z[1];
            z[1] = c[2] * x - c[4] * y;
            x = y;
        }
        pcm[i] = x >= 32767.0f ? 32767 : x <= -32768.0f ? -32768 : (int16_t) x;
    }
}
