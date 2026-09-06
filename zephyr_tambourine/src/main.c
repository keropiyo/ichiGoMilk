#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>
#include <zephyr/irq.h>
#include <stdint.h>

#define SAMPLE_PERIOD_MS 10

#define HIT_THRESHOLD_MG_PER_SAMPLE 650
#define HIT_RELEASE_MG_PER_SAMPLE 600
#define HIT_COOLDOWN_MS 55
#define HIT_RELEASE_SAMPLES 1

#define RAW_INTERVAL_MS 100

/* ===== LED設定 ===== */

/* Hitした時に白く光る時間 */
#define HIT_LED_MS 120

/* 光らせるLED数 */
#define LED_COUNT 6

/* PTA12 */
#define LED_GPIO_NODE DT_NODELABEL(gpioa)
#define LED_PIN 12

#define GPIOA_PSOR (*(volatile uint32_t *)0x400FF004)
#define GPIOA_PCOR (*(volatile uint32_t *)0x400FF008)
#define PIN12 (1u << 12)

#define HI() GPIOA_PSOR = PIN12
#define LO() GPIOA_PCOR = PIN12

static const struct device *const led_gpio = DEVICE_DT_GET(LED_GPIO_NODE);

static int64_t hit_led_until = 0;
static int current_led_mode = -1;

/*
 * WS2812B 1bit送信
 * 色が不安定なら nop 数を微調整する
 */
static inline void send_bit_1(void)
{
    HI();
    __asm__ volatile(
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
    );
    LO();
    __asm__ volatile(
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
    );
}

static inline void send_bit_0(void)
{
    HI();
    __asm__ volatile(
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
    );
    LO();
    __asm__ volatile(
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
        "nop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\nnop\n"
    );
}

static void send_byte(uint8_t b)
{
    for (int i = 7; i >= 0; i--) {
        if (b & (1 << i)) {
            send_bit_1();
        } else {
            send_bit_0();
        }
    }
}

/* WS2812Bは GRB 順 */
static void send_pixel(uint8_t r, uint8_t g, uint8_t b)
{
    send_byte(g);
    send_byte(r);
    send_byte(b);
}

static void show_two_leds(uint8_t r, uint8_t g, uint8_t b)
{
    unsigned int key = irq_lock();

    for (int i = 0; i < LED_COUNT; i++) {
        send_pixel(r, g, b);
    }

    irq_unlock(key);

    /* ラッチ時間 */
    k_busy_wait(80);
}

static void led_show_mode(int mode)
{
    if (mode == current_led_mode) {
        return;
    }

    current_led_mode = mode;

    if (mode == 0) {
        /* OFF */
        show_two_leds(0, 0, 0);
    } else if (mode == 1) {
        /* 通常時：ピンク */
        show_two_leds(45, 0, 18);
    } else if (mode == 2) {
        /* Hit時：白 */
        show_two_leds(90, 90, 90);
    }
}

static void led_update(int64_t now)
{
    if (now < hit_led_until) {
        /* Hit直後は白を優先 */
        led_show_mode(2);
    } else {
        /* 通常時はピンクで常時点灯 */
        led_show_mode(1);
    }
}

/* ===== センサー設定 ===== */

#define ACCEL_NODE DT_COMPAT_GET_ANY_STATUS_OKAY(nxp_fxls8974)

#if !DT_NODE_HAS_STATUS(ACCEL_NODE, okay)
#error "No enabled nxp,fxls8974 accelerometer node found in devicetree"
#endif

static const struct device *const accel = DEVICE_DT_GET(ACCEL_NODE);

/*
 * Zephyr 4.4.99のFXLS8974ドライバは、ACTIVE中のODR変更が
 * センサーに反映されない。いったんSTANDBYにして100Hzへ変更する。
 */
extern int fxls8974_set_active(const struct device *dev, uint8_t active);

static int configure_accel_100hz(void)
{
    struct sensor_value odr = {
        .val1 = 100,
        .val2 = 0,
    };
    int ret;

    ret = fxls8974_set_active(accel, 0);
    if (ret != 0) {
        return ret;
    }

    ret = sensor_attr_set(accel, SENSOR_CHAN_ALL,
                          SENSOR_ATTR_SAMPLING_FREQUENCY, &odr);
    if (ret != 0) {
        (void)fxls8974_set_active(accel, 1);
        return ret;
    }

    return fxls8974_set_active(accel, 1);
}

static int sensor_value_to_mg(const struct sensor_value *v)
{
    double ms2 = sensor_value_to_double(v);
    return (int)(ms2 * 1000.0 / 9.80665);
}

static int iabs(int v)
{
    return v < 0 ? -v : v;
}

int main(void)
{
    struct sensor_value xyz[3];

    int last_x = 0;
    int last_y = 0;
    int last_z = 0;

    int filtered_energy = 0;
    int initialized = 0;

    int hit_armed = 1;
    int release_count = 0;

    int64_t last_hit = -100000;
    int64_t last_raw = 0;

    printk("{\"type\":\"boot\",\"app\":\"ichigo-milk-tambourine\"}\n");

    if (!device_is_ready(accel)) {
        printk("{\"type\":\"error\",\"message\":\"accelerometer_not_ready\"}\n");
        return 0;
    }

    int odr_ret = configure_accel_100hz();
    if (odr_ret != 0) {
        printk("{\"type\":\"error\",\"message\":\"accelerometer_100hz_failed\",\"ret\":%d}\n",
               odr_ret);
        return 0;
    }

    printk("{\"type\":\"sensor_config\",\"odr_hz\":100}\n");

    if (!device_is_ready(led_gpio)) {
        printk("{\"type\":\"error\",\"message\":\"led_gpio_not_ready\"}\n");
        return 0;
    }

    gpio_pin_configure(led_gpio, LED_PIN, GPIO_OUTPUT_INACTIVE);

    show_two_leds(0, 0, 0);

    printk("{\"type\":\"ready\",\"sensor\":\"%s\",\"sample_ms\":%d,\"threshold\":%d,\"release\":%d,\"cooldown\":%d,\"release_samples\":%d}\n",
           accel->name,
           SAMPLE_PERIOD_MS,
           HIT_THRESHOLD_MG_PER_SAMPLE,
           HIT_RELEASE_MG_PER_SAMPLE,
           HIT_COOLDOWN_MS,
           HIT_RELEASE_SAMPLES);

    while (1) {
        int64_t now = k_uptime_get();

        /* まずLED状態を更新 */
        led_update(now);

        int ret = sensor_sample_fetch(accel);
        if (ret != 0) {
            printk("{\"type\":\"error\",\"message\":\"sensor_sample_fetch\",\"ret\":%d}\n", ret);
            k_sleep(K_MSEC(250));
            continue;
        }

        ret = sensor_channel_get(accel, SENSOR_CHAN_ACCEL_XYZ, xyz);
        if (ret != 0) {
            printk("{\"type\":\"error\",\"message\":\"sensor_channel_get\",\"ret\":%d}\n", ret);
            k_sleep(K_MSEC(250));
            continue;
        }

        int x = sensor_value_to_mg(&xyz[0]);
        int y = sensor_value_to_mg(&xyz[1]);
        int z = sensor_value_to_mg(&xyz[2]);

        if (!initialized) {
            last_x = x;
            last_y = y;
            last_z = z;
            initialized = 1;
        }

        int energy = iabs(x - last_x) + iabs(y - last_y) + iabs(z - last_z);

        last_x = x;
        last_y = y;
        last_z = z;

        filtered_energy = (filtered_energy * 7 + energy) / 8;

        int hit_power = energy - filtered_energy;
        now = k_uptime_get();

        if (!hit_armed) {
            if (hit_power < HIT_RELEASE_MG_PER_SAMPLE) {
                release_count++;
                if (release_count >= HIT_RELEASE_SAMPLES) {
                    hit_armed = 1;
                }
            } else {
                release_count = 0;
            }
        } else if (hit_power > HIT_THRESHOLD_MG_PER_SAMPLE &&
                   (now - last_hit) > HIT_COOLDOWN_MS) {
            last_hit = now;
            hit_armed = 0;
            release_count = 0;

            /* Hitしたら白く光らせる */
            hit_led_until = now + HIT_LED_MS;
            led_show_mode(2);

            printk("{\"type\":\"hit\",\"ms\":%lld,\"power\":%d,\"x\":%d,\"y\":%d,\"z\":%d,\"energy\":%d,\"filtered\":%d}\n",
                   now,
                   hit_power,
                   x,
                   y,
                   z,
                   energy,
                   filtered_energy);
        }

        if ((now - last_raw) > RAW_INTERVAL_MS) {
            last_raw = now;

            printk("{\"type\":\"raw\",\"ms\":%lld,\"x\":%d,\"y\":%d,\"z\":%d,\"energy\":%d,\"filtered\":%d,\"power\":%d,\"armed\":%d}\n",
                   now,
                   x,
                   y,
                   z,
                   energy,
                   filtered_energy,
                   hit_power,
                   hit_armed);
        }

        /* 最後にもLED状態を更新 */
        led_update(now);

        k_sleep(K_MSEC(SAMPLE_PERIOD_MS));
    }
}
