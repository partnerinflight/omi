#include "lib/core/dock.h"

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#include "lib/core/mic.h"

LOG_MODULE_REGISTER(dock, CONFIG_LOG_DEFAULT_LEVEL);

extern bool is_charging; /* main.c, maintained by the CHG interrupt */

static const struct gpio_dt_spec vin_detect = GPIO_DT_SPEC_GET_OR(DT_NODELABEL(vin_detect_pin), gpios, {0});
static struct gpio_callback vin_detect_cb;
static atomic_t present;
static bool ready;

static void dock_settle(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(dock_work, dock_settle);

bool dock_vin_detect_raw(void)
{
    return ready && gpio_pin_get_dt(&vin_detect) > 0;
}

bool dock_power_present(void)
{
    return atomic_get(&present) != 0;
}

/* Runs once the level has been stable for the debounce period. */
static void dock_settle(struct k_work *work)
{
    ARG_UNUSED(work);
    bool now = dock_present_from(dock_vin_detect_raw(), is_charging);
    if ((atomic_set(&present, now) != 0) == now) {
        return;
    }
    LOG_INF("Dock power %s", now ? "present" : "removed");
    mic_set_docked(now);
}

void dock_recheck(void)
{
    k_work_reschedule(&dock_work, K_MSEC(CONFIG_OMI_DOCK_DEBOUNCE_MS));
}

void dock_poll(void)
{
    if (dock_present_from(dock_vin_detect_raw(), is_charging) != dock_power_present()) {
        k_work_schedule(&dock_work, K_MSEC(CONFIG_OMI_DOCK_DEBOUNCE_MS));
    }
}

static void vin_detect_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
    ARG_UNUSED(dev);
    ARG_UNUSED(cb);
    ARG_UNUSED(pins);
    dock_recheck();
}

int dock_init(void)
{
    int err = 0;

    if (vin_detect.port != NULL && gpio_is_ready_dt(&vin_detect)) {
        /* No internal pull: it would load the divider and drag a docked level under VIH. */
        err = gpio_pin_configure_dt(&vin_detect, GPIO_INPUT);
        if (!err) {
            gpio_init_callback(&vin_detect_cb, vin_detect_isr, BIT(vin_detect.pin));
            err = gpio_add_callback(vin_detect.port, &vin_detect_cb);
        }
        if (!err) {
            err = gpio_pin_interrupt_configure_dt(&vin_detect, GPIO_INT_EDGE_BOTH);
        }
        ready = !err;
        if (err) {
            LOG_ERR("VIN_DETECT setup failed (%d); using the charge signal only", err);
        }
    } else {
        LOG_WRN("No VIN_DETECT pin; using the charge signal only");
    }

    bool now = dock_present_from(dock_vin_detect_raw(), is_charging);
    atomic_set(&present, now);
    LOG_INF("Dock power %s at boot (VIN_DETECT %d)", now ? "present" : "absent", dock_vin_detect_raw());
    mic_set_docked(now);
    return err;
}
