#ifndef OMI_BULK_OWNER_H
#define OMI_BULK_OWNER_H
#include <stdatomic.h>
#include <stdbool.h>
enum bulk_owner { BULK_FREE, BULK_BLE, BULK_WIFI };
static inline bool bulk_claim(atomic_int *owner, enum bulk_owner wanted)
{
    int expected = BULK_FREE;
    return atomic_compare_exchange_strong(owner, &expected, wanted);
}
static inline void bulk_release(atomic_int *owner, enum bulk_owner current)
{
    int expected = current;
    atomic_compare_exchange_strong(owner, &expected, BULK_FREE);
}
#endif
