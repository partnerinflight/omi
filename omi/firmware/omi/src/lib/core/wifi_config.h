#ifndef OMI_WIFI_CONFIG_H
#define OMI_WIFI_CONFIG_H
#include "wifi_upload.h"
/* Pure parser: rejects the entire update before changing the caller's value. */
int wifi_config_parse(struct wifi_upload_config *cfg, const uint8_t *buf, size_t len);
bool wifi_config_valid(const struct wifi_upload_config *cfg);
bool wifi_hostname_valid(const char *host, size_t len);
#endif
