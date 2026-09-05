#ifndef STORAGE_H
#define STORAGE_H

#ifdef CONFIG_OMI_ENABLE_OFFLINE_STORAGE

#include <stdbool.h>
#include <zephyr/bluetooth/uuid.h>

/** Local-only recorder storage service UUID (advertised; used by the dump CLI). */
extern struct bt_uuid_128 local_storage_service_uuid;

/**
 * @brief Initializes the Storage Transport thread
 *
 * Initializes the Storage Transport thread
 *
 * @return 0 if successful, negative errno code if error
 */
int storage_init();

/**
 * @brief Stops the current storage transfer
 *
 * Stops the current storage transfer
 */
void storage_stop_transfer();

/**
 * @brief Returns true when storage sync transfer is active.
 */
bool storage_transfer_active(void);

#endif // CONFIG_OMI_ENABLE_OFFLINE_STORAGE

#endif // STORAGE_H
