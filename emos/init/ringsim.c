/* Render the boot ring off-target, so the animation can be judged and its
 * invariants checked without flashing a device.
 *
 * The ring is the one part of emOS whose whole purpose is how it LOOKS, and it
 * appears for thirty seconds on hardware that has to be reflashed to change
 * it. So it is driven here instead: init.c is included whole — the animation
 * functions are static — with LEDDIR pointed at a temp file and main renamed.
 * Nothing is reimplemented, so this cannot drift from what the device runs.
 *
 *   cc -O2 -o ringsim ringsim.c && ./ringsim          # frames as text
 *   ./ringsim --check                                 # invariants only
 *
 * Timing is simulated, not slept, so a 30-second boot checks in milliseconds.
 */
/* Headers first: init.c's own includes then no-op behind their guards, so the
 * usleep macro below cannot rewrite unistd.h's prototype. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define LEDDIR "/tmp/emos-ringsim"
#define main   init_main_unused
#define usleep sim_usleep

static void sim_usleep(unsigned us);

#include "init.c"

#undef main
#undef usleep

/* Time is simulated rather than slept, and every frame the animation writes is
 * followed by a sleep — so capturing here catches all of them, including the
 * finale's, without a hook inside the animation itself. */
static void capture(void);
static long sim_now_us;
static void sim_usleep(unsigned us) { capture(); sim_now_us += us; }

/* ── Capture ────────────────────────────────────────────────────────────────*/

#define MAXFR 20000
static struct { unsigned char rgb[LED_N][3]; long t; } frames[MAXFR];
static int nframes;

/* led_write goes to a file; read it back so the check sees exactly the bytes
 * the driver would. */
static void capture(void)
{
    FILE *f = fopen(LEDDIR "/frame", "rb");
    if (!f) return;
    char hex[LED_N * 6 + 1];
    size_t n = fread(hex, 1, LED_N * 6, f);
    fclose(f);
    if (n != LED_N * 6 || nframes >= MAXFR) return;
    for (int i = 0; i < LED_N; i++) {
        int p = (LED_BOTTOM + LED_DIR * i) % LED_N;
        if (p < 0) p += LED_N;
        unsigned r, g, b;
        sscanf(hex + p * 6, "%2x%2x%2x", &r, &g, &b);
        frames[nframes].rgb[i][0] = r;
        frames[nframes].rgb[i][1] = g;
        frames[nframes].rgb[i][2] = b;
    }
    frames[nframes].t = sim_now_us;
    nframes++;
}

/* ── A boot, as it actually runs ────────────────────────────────────────────*/

/* Measured shape of a real boot: most stages are instant, three are not.
 * Milliseconds spent IN each stage, i.e. before it completes. */
static const int stage_ms[LED_N] = {
    120,    /*  1 mounts and device nodes */
    260,    /*  2 /system                 */
    4200,   /*  3 fsck /data              — seconds, and silent */
    340,    /*  4 /data                   */
    60,     /*  5 /etc                    */
    40,     /*  6 last_kmsg               */
    90,     /*  7 busybox applets         */
    150,    /*  8 USB gadget              */
    1100,   /*  9 console                 */
    2600,   /* 10 wlan0 exists            */
    9000,   /* 11 associating             — the long silent one */
    900,    /* 12 address                 */
};

static void run_boot(int fail_at)
{
    ledst = calloc(1, sizeof *ledst);
    ledst->mode = ANIM_RUN;
    bootstep = 0;

    int head_q = 0, tick = 0;
    for (int s = 0; s < LED_N; s++) {
        int ticks = stage_ms[s] / TICK_MS;
        for (int k = 0; k < ticks; k++) {
            int stage = ledst->stage;
            int target = stage * SUB + (stage < LED_N ? CREEP : 0);
            if (head_q < target) {
                int step = (target - head_q) / 8;
                if (step < 1) step = 1;
                head_q += step;
                if (head_q > target) head_q = target;
            }
            anim_render(head_q, tick++, 0);
            sim_usleep(TICK_MS * 1000);
        }
        if (fail_at == s + 1) {
            for (int k = 0; k < 30; k++) {
                anim_render(head_q, tick++, 1);
                sim_usleep(TICK_MS * 1000);
            }
            return;
        }
        led_step();
    }
    anim_finale(head_q, tick);
    capture();   /* the finale's last frame is black and has no sleep after it */
}

/* ── Output ────────────────────────────────────────────────────────────────*/

static const char *ramp = ".:-=+*#%@";

/* Gamma-corrected, because the eye is not linear and neither is an LED at low
 * drive: a 0x30 blue that computes to 4% of full luminance is plainly visible
 * on the ring. Rendering the ramp linearly showed the dark blue trail as
 * blank, which would have had me brightening a trail that is probably fine. */
static void show(void)
{
    for (int i = 0; i < nframes; i++) {
        printf("%6ldms ", frames[i].t / 1000);
        for (int p = 0; p < LED_N; p++) {
            const unsigned char *c = frames[i].rgb[p];
            int lum = (c[0] * 30 + c[1] * 59 + c[2] * 11) / 100;
            int red = c[0] > 100 && c[2] < 60;
            if (!lum) { putchar(' '); continue; }
            int v = (int)(pow(lum / 255.0, 1 / 2.2) * 8.999);
            putchar(red ? 'R' : ramp[v]);
        }
        printf("\n");
    }
}

/* ── Invariants ────────────────────────────────────────────────────────────*/

static int fails;
static void ck(int ok, const char *what)
{
    printf("%-58s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) fails++;
}

/* Position of the head: the brightest segment, in logical order. */
static int headpos(int i)
{
    int best = 0, bl = -1;
    for (int p = 0; p < LED_N; p++) {
        const unsigned char *c = frames[i].rgb[p];
        int lum = c[0] * 30 + c[1] * 59 + c[2] * 11;
        if (lum > bl) { bl = lum; best = p; }
    }
    return best;
}

static void check_run(void)
{
    /* The head must never be still for long: that is the entire design goal,
     * and the stages it has to survive are fsck (4.2s) and association (9s). */
    int worst = 0, run = 0;
    for (int i = 1; i < nframes; i++) {
        if (memcmp(frames[i].rgb, frames[i - 1].rgb, sizeof frames[i].rgb) == 0)
            run++;
        else
            run = 0;
        if (run > worst) worst = run;
    }
    printf("longest identical run: %d frames (%dms)\n", worst, worst * TICK_MS);
    ck(worst * TICK_MS < 400, "ring is never still for 400ms");

    /* Progress is monotonic and never overtakes the boot. */
    int back = 0;
    for (int i = 1; i < nframes; i++)
        if (headpos(i) < headpos(i - 1) && headpos(i - 1) - headpos(i) < LED_N / 2)
            back++;
    ck(back == 0, "head never travels backwards");

    /* Everything behind the head is trail, nothing ahead of it is lit. */
    int ahead = 0;
    for (int i = 0; i < nframes / 2; i++) {
        int h = headpos(i);
        for (int p = h + 2; p < LED_N; p++)
            if (frames[i].rgb[p][0] | frames[i].rgb[p][1] | frames[i].rgb[p][2])
                ahead++;
    }
    ck(ahead == 0, "nothing lights ahead of the head");

    /* The ring must be handed back dark. A ring left lit is the thing the fade
     * exists to prevent — anything shown after this is the firmware's. */
    int lit = 0;
    for (int p = 0; p < LED_N; p++)
        lit |= frames[nframes - 1].rgb[p][0] | frames[nframes - 1].rgb[p][1]
             | frames[nframes - 1].rgb[p][2];
    ck(!lit, "the ring is handed back dark");
}

int main(int argc, char **argv)
{
    /* led_write opens the frame O_WRONLY with no O_CREAT, which is correct for
     * a sysfs attribute and means the stand-in has to exist first. It did not,
     * so the first run of this captured nothing and every check below passed
     * on an empty array — hence the count assertion. */
    mkdir(LEDDIR, 0755);
    close(open(LEDDIR "/frame", O_WRONLY | O_CREAT, 0644));

    int check = argc > 1 && !strcmp(argv[1], "--check");
    int fail  = argc > 2 ? atoi(argv[2]) : 0;

    run_boot(fail);
    if (!check) { show(); return 0; }

    printf("frames=%d simulated=%ldms\n\n", nframes, sim_now_us / 1000);
    ck(nframes > 100, "the animation actually produced frames");
    check_run();
    printf("\n%s\n", fails ? "FAILED" : "all ok");
    return fails ? 1 : 0;
}
