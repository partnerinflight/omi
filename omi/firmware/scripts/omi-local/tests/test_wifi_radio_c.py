"""Compile the production radio lifecycle against a delayed supplicant/hardware seam."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
STUB = r'''
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#define CONFIG_NET_SOCKETS_POLL_MAX 16
#define CONFIG_NET_MGMT_EVENT_STACK_SIZE 8192
struct device {int unused;};
struct gpio_dt_spec {int pin;};
#define bucken_gpios 1
#define iovdd_ctrl_gpios 2
#define GPIO_DT_SPEC_GET(node,prop) {.pin=prop}
#define GPIO_OUTPUT_INACTIVE 0
bool gpio_is_ready_dt(const struct gpio_dt_spec *s);
int gpio_pin_configure_dt(const struct gpio_dt_spec *s,int flags);
int gpio_pin_set_dt(const struct gpio_dt_spec *s,int value);
int nrf_wifi_if_zep_start_board(const struct device *dev);
struct net_if {bool up;};
struct wifi_nm_instance {int unused;};
#define K_MSEC(x) (x)
#define NET_LINK_ETHERNET 1
int64_t k_uptime_get(void);
void k_sleep(int ms);
ssize_t hwinfo_get_device_id(uint8_t *buf, size_t size);
struct wifi_nm_instance *wifi_nm_get_instance_iface(struct net_if *iface);
int net_if_down(struct net_if *iface);
int net_if_up(struct net_if *iface);
bool net_if_is_admin_up(struct net_if *iface);
int net_if_set_link_addr(struct net_if *iface, uint8_t *addr, uint8_t len, int type);
'''
HARNESS = r'''
#include "stub.h"
#include "lib/core/wifi_radio.h"
#include "lib/core/wifi_mac.h"
#include <stdio.h>
#include <string.h>
#include <errno.h>
static int64_t now, change_at;
static bool managed, stuck, fail_id;
static uint8_t mac[6];
static int reset_order;
static struct net_if iface;
static struct wifi_nm_instance nm;
int64_t k_uptime_get(void) {return now;}
void k_sleep(int ms) {now+=ms; if(now>=change_at) managed=iface.up&&!stuck;}
ssize_t hwinfo_get_device_id(uint8_t *buf,size_t size) {
 const uint8_t id[]={0xf1,2,3,4,5,6,7,8};
 if(fail_id) return -EIO;
 if(size<sizeof(id)) return -EINVAL;
 memcpy(buf,id,sizeof(id));return sizeof(id);
}
struct wifi_nm_instance *wifi_nm_get_instance_iface(struct net_if *i) {(void)i;return managed?&nm:NULL;}
int net_if_down(struct net_if *i) {i->up=false;change_at=now+100;return 0;}
bool gpio_is_ready_dt(const struct gpio_dt_spec *s) {(void)s;return true;}
int gpio_pin_configure_dt(const struct gpio_dt_spec *s,int flags) {
 if(flags!=GPIO_OUTPUT_INACTIVE) return -EINVAL;
 reset_order=reset_order*10+s->pin;return 0;
}
int gpio_pin_set_dt(const struct gpio_dt_spec *s,int value) {
 if(value!=1) return -EINVAL;
 reset_order=reset_order*10+s->pin;return 0;
}
int net_if_up(struct net_if *i) {
 int64_t before=now;reset_order=0;
 int ret=nrf_wifi_if_zep_start_board(NULL);
 if(ret||reset_order!=1212||now-before<31) return -EINVAL;
 i->up=true;change_at=now+100;return 0;
}
bool net_if_is_admin_up(struct net_if *i) {return i->up;}
int net_if_set_link_addr(struct net_if *i,uint8_t *addr,uint8_t len,int type) {
 (void)type;if(i->up||len!=6) return -EINVAL;memcpy(mac,addr,6);return 0;
}
int main(int argc,char **argv) {
 (void)argc;
 if(!strcmp(argv[1],"timeout")) {stuck=true;int ret=wifi_radio_start(&iface);return ret==-ETIMEDOUT&&!iface.up?0:1;}
 if(!strcmp(argv[1],"no-id")) {fail_id=true;return wifi_radio_start(&iface)==-EIO&&!iface.up?0:2;}
 if(!strcmp(argv[1],"short-id")) {uint8_t id[5]={0};return !wifi_mac_from_id(id,5,mac)?0:3;}
 managed=true;change_at=100;
 if(wifi_radio_start(&iface)||!managed||now<200||(mac[0]&3)!=2) return 4;
 uint8_t before[6];memcpy(before,mac,6);
 wifi_radio_stop(&iface);
 if(managed||iface.up) return 5;
 if(wifi_radio_start(&iface)||memcmp(before,mac,6)) return 6;
 const uint8_t expected[]={0xf6,10,3,4,5,6};
 if(memcmp(mac,expected,6)) return 7;
 return 0;
}
'''

@unittest.skipUnless(shutil.which('cc'),'native C compiler required')
class WifiRadioTests(unittest.TestCase):
    def test_compiler_rejects_insufficient_supplicant_poll_capacity(self):
        # Static configuration guard, grounded in the device's eloop ENOMEM
        # and NCS 2.9 socket Kconfig's six-entry supplicant minimum.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'limits.c'
            source.write_text('#include "lib/core/wifi_limits.h"\n')
            for count, stack, success in [(4,8192,False), (6,8192,True), (16,8192,True), (16,4200,False)]:
                result = subprocess.run(['cc', '-I', str(SRC),
                    '-DCONFIG_NET_SOCKETS_POLL_MAX=' + str(count),
                    '-DCONFIG_NET_MGMT_EVENT_STACK_SIZE=' + str(stack), '-c', str(source),
                    '-o', str(root / 'limits.o')], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_device_mac_and_delayed_owner_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'stub.h').write_text(STUB);(root/'main.c').write_text(HARNESS)
            for name in ['drivers/hwinfo.h','drivers/gpio.h','kernel.h','net/net_if.h','net/wifi_nm.h']:
                p=root/'zephyr'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('#include "stub.h"\n')
            exe=root/'check'
            subprocess.run(['cc','-Wall','-Wextra','-Werror','-I',str(root),'-I',str(SRC),str(root/'main.c'),str(SRC/'wifi_radio.c'),'-o',str(exe)],check=True)
            for scenario in ['ready','timeout','no-id','short-id']:
                with self.subTest(scenario=scenario): subprocess.run([str(exe),scenario],check=True)
