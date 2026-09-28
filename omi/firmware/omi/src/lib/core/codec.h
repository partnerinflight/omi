#ifndef CODEC_H
#define CODEC_H
#include <zephyr/kernel.h>

// Callback
/* len == 0 is an ordered recording-end marker, not an Opus packet. */
typedef int (*codec_callback)(uint8_t *data, size_t len);
void set_codec_callback(codec_callback callback);

// Integration

int codec_receive_pcm(int16_t *data, size_t len);
/* Call only after pausing the PCM producer. Drains audio before the marker. */
int codec_end_recording(void);

/**
 * @brief Initialize the Codec
 *
 * Initializes the codec
 *
 * @return 0 if successful, negative errno code if error
 */
int codec_start();

#endif
