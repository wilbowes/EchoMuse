/* emreboot — reboot into a boot mode on a device whose init cannot yet do it.
 *
 * `/init recovery` is the supported way to reach TWRP from emOS, and on any
 * device running an init from this tree onward that is what you should use.
 * This exists for the devices that predate it: emos-v0.3 and earlier have no
 * multi-call init, and a device on emOS has no adb, so the only ways in are a
 * console paste or an HTTP fetch over its own WiFi.
 *
 * Freestanding for that reason — no libc, 680 bytes, small enough that its
 * base64 pastes through a serial console in one go. Size is the only design
 * constraint here, and it is the one that decides whether this is usable at
 * all on a device you cannot copy a file to.
 *
 * Not built by CI and not part of the image. Build it with the pinned
 * compiler:
 *
 *   docker run --rm -v "$PWD/emos":/emos -v /tmp:/out -w /emos echomuse-compiler \
 *     bash -lc '$NDK/aarch64-linux-android21-clang -nostdlib -static -Os \
 *               -o /out/emreboot tools/emreboot.c' && llvm-strip /tmp/emreboot
 *
 * What it does is one syscall: RESTART2 carrying a mode string, which
 * MediaTek's restart handler turns into the value LK reads on the next boot.
 * Amazon's /system/bin/reboot cannot do this under emOS — it reaches Android's
 * property service over a socket that does not exist here and fails with
 * ENOENT for every mode, including no mode at all. See emos/README.md.
 */
#define MAGIC1 0xfee1dead
#define MAGIC2 0x28121969
#define RESTART2 0xA1B2C3D4
#define NR_reboot 142
#define NR_write  64
#define NR_exit   93
#define NR_sync   81

static long sys4(long n, long a, long b, long c, long d)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    __asm__ volatile("svc #0" : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory");
    return x0;
}

static const char mode[] = "recovery";
static const char msg[]  = "emreboot: RESTART2 recovery\n";
static const char bad[]  = "emreboot: kernel refused RESTART2\n";

void _start(void)
{
    sys4(NR_write, 1, (long)msg, sizeof msg - 1, 0);
    sys4(NR_sync, 0, 0, 0, 0);
    sys4(NR_reboot, MAGIC1, MAGIC2, RESTART2, (long)mode);
    /* Only reached if the kernel refused the command string. */
    sys4(NR_write, 2, (long)bad, sizeof bad - 1, 0);
    sys4(NR_exit, 1, 0, 0, 0);
    for (;;) { }
}
