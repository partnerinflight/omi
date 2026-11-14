#include "lib/core/wifi_radio.h"

#include <errno.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/kernel.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/wifi_nm.h>

#include "lib/core/wifi_limits.h"
#include "lib/core/wifi_mac.h"

/* Nordic's board startup hook runs before its RPU power-on sequence. A
 * firmware soft reset need not discharge the external chip's power rails;
 * force a cold peripheral start before the driver establishes QSPI state. */
int nrf_wifi_if_zep_start_board(const struct device *dev)
{
    (void) dev;
    static const struct gpio_dt_spec buck = GPIO_DT_SPEC_GET(DT_NODELABEL(nrf70), bucken_gpios);
    static const struct gpio_dt_spec io = GPIO_DT_SPEC_GET(DT_NODELABEL(nrf70), iovdd_ctrl_gpios);
    if (!gpio_is_ready_dt(&buck) || !gpio_is_ready_dt(&io))
        return -ENODEV;
    int ret = gpio_pin_configure_dt(&buck, GPIO_OUTPUT_INACTIVE);
    if (!ret)
        ret = gpio_pin_configure_dt(&io, GPIO_OUTPUT_INACTIVE);
    if (ret)
        return ret;
    k_sleep(K_MSEC(20));
    ret = gpio_pin_set_dt(&buck, 1);
    if (ret)
        return ret;
    k_sleep(K_MSEC(1));
    ret = gpio_pin_set_dt(&io, 1);
    if (ret) {
        gpio_pin_set_dt(&buck, 0);
        return ret;
    }
    /* CV1 U11 is TPS22916C (slow turn-on: 1.7 ms typical at 3.6 V),
     * not the DK's TCK106AG assumed by NCS's 1 ms wait. Settle the CV1
     * rail here before rpu_init preserves the already-active outputs. */
    k_sleep(K_MSEC(10));
    return 0;
}

/* Mirror of the SDK's rpu_pwroff(): BUCKEN low, then IOVDD. rpu_pwroff only runs when the
 * driver removes a fully started device; our start hook raises both rails on every
 * bring-up, so without this a partial start or a context-less stop leaves them up. */
static void rails_off(void)
{
    static const struct gpio_dt_spec buck = GPIO_DT_SPEC_GET(DT_NODELABEL(nrf70), bucken_gpios);
    static const struct gpio_dt_spec io = GPIO_DT_SPEC_GET(DT_NODELABEL(nrf70), iovdd_ctrl_gpios);
    if (gpio_is_ready_dt(&buck))
        (void) gpio_pin_set_dt(&buck, 0);
    if (gpio_is_ready_dt(&io))
        (void) gpio_pin_set_dt(&io, 0);
}

/* Board-specific shutdown, run by the driver at the end of every interface stop. */
int nrf_wifi_if_zep_stop_board(const struct device *dev)
{
    (void) dev;
    rails_off();
    return 0;
}

static uint8_t local_mac[6];

static int wait_managed(struct net_if *iface, bool ready)
{
    int64_t deadline = k_uptime_get() + 10000;
    while ((wifi_nm_get_instance_iface(iface) != NULL) != ready) {
        if (k_uptime_get() >= deadline)
            return -ETIMEDOUT;
        k_sleep(K_MSEC(25));
    }
    return 0;
}

int wifi_radio_stop(struct net_if *iface)
{
    if (!iface)
        return 0;
    net_if_down(iface);
    /* NCS removes the supplicant interface asynchronously. Its removal
     * must finish before the owner starts the next AP/STA session. */
    return wait_managed(iface, false);
}

int wifi_radio_start(struct net_if *iface)
{
    if (!iface)
        return -ENODEV;
    if (!net_if_is_admin_up(iface)) {
        int ret = wait_managed(iface, false);
        if (ret)
            return ret;
        uint8_t id[16];
        ssize_t length = hwinfo_get_device_id(id, sizeof(id));
        if (length < 0)
            return length;
        if (!wifi_mac_from_id(id, length, local_mac))
            return -ENODEV;
        /* The CV1 tested on hardware returned an all-zero OTP MAC. The
         * Nordic driver accepts a valid link address supplied before start. */
        ret = net_if_set_link_addr(iface, local_mac, sizeof(local_mac), NET_LINK_ETHERNET);
        if (ret)
            return ret;
        ret = net_if_up(iface);
        if (ret && ret != -EALREADY) {
            /* The driver runs no stop after a failed start, but our start hook has
             * already powered the radio. */
            rails_off();
            return ret;
        }
    }
    /* Interface-up is not supplicant-ready: registration happens on its
     * event/work queue. Scan/AP/connect must wait for that authoritative owner. */
    int ret = wait_managed(iface, true);
    if (ret)
        wifi_radio_stop(iface);
    return ret;
}
