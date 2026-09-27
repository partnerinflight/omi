#pragma once
struct net_if;
int wifi_radio_start(struct net_if *iface);
void wifi_radio_stop(struct net_if *iface);
