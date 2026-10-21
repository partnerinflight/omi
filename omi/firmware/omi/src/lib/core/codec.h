#ifndef CODEC_H
#define CODEC_H
#include <zephyr/kernel.h>

// Callback
/* len == 0 is an ordered recording-end marker, not an Opus packet. */
typedef int (*codec_callback)(uint8_t *data, size_t len);
void set_codec_callback(codec_callback callback);
/* Ordered recording-mode marker (record_packer.h RECORDING_MODE_*). */
typedef int (*codec_mode_callback)(uint8_t mode);
void set_codec_mode_callback(codec_mode_callback callback);

// Integration

int codec_receive_pcm(int16_t *data, size_t len);
/* Call only after pausing the PCM producer. Drains audio before the marker. */
int codec_end_recording(void);
/* Same contract: call with the PCM producer paused; returns once the marker is written.
 * codec_end_recording and codec_mark_mode share one completion semaphore and result:
 * never call them concurrently. Only the microphone owner thread calls them (asserted
 * with CONFIG_ASSERT). */
int codec_mark_mode(uint8_t mode);

/**
 * @brief Initialize the Codec
 *
 * Initializes the codec
 *
 * @return 0 if successful, negative errno code if error
 */
int codec_start();

#endif
