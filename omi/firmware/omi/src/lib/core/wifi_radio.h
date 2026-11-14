#pragma once
struct net_if;
int wifi_radio_start(struct net_if *iface);
/* Brings the interface down; the driver's stop ends in the board hook, which powers the
 * nRF7002 rails off. Returns -ETIMEDOUT if the supplicant did not release the interface. */
int wifi_radio_stop(struct net_if *iface);
