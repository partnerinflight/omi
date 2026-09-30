#include "vox_filter.h"

#include <string.h>

/* RBJ cookbook coefficients at 16 kHz, Q = 1/sqrt(2): b0, b1, b2, a1, a2 (a0 normalised). */
static const float HIGH_PASS_250[5] = {0.932932156f, -1.865864312f, 0.932932156f, -1.861361147f, 0.870367477f};
static const float LOW_PASS_3400[5] = {0.227117964f, 0.454235928f, 0.227117964f, -0.276664615f, 0.185136470f};

static float biquad(struct vox_biquad *s, const float c[5], float x)
{
    /* Transposed direct form II. */
    float y = c[0] * x + s->z1;
    s->z1 = c[1] * x - c[3] * y + s->z2;
    s->z2 = c[2] * x - c[4] * y;
    return y;
}

void vox_filter_reset(struct vox_filter *f)
{
    memset(f, 0, sizeof(*f));
}

uint32_t vox_block_level(struct vox_filter *f, const int16_t *pcm, size_t frames)
{
    if (frames == 0) {
        return 0;
    }
    float sum = 0.0f;
    for (size_t i = 0; i < frames; i++) {
        float y = biquad(&f->lp, LOW_PASS_3400, biquad(&f->hp, HIGH_PASS_250, (float) pcm[i]));
        sum += y < 0.0f ? -y : y;
    }
    return (uint32_t) (sum / (float) frames);
}

bool vox_voice_detected(struct vox_filter *f, uint32_t level, uint32_t threshold, unsigned sustain, unsigned window)
{
    uint8_t mask = window >= 8 ? 0xFF : (uint8_t) ((1U << window) - 1U);
    f->history = (uint8_t) (((unsigned) f->history << 1 | (level >= threshold ? 1U : 0U)) & mask);
    unsigned loud = 0;
    for (uint8_t bits = f->history; bits; bits &= (uint8_t) (bits - 1)) {
        loud++;
    }
    return loud >= sustain;
}
