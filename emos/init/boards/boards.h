/*
 * The board-runtime interface.
 *
 * init.c calls into the selected board file through prototypes declared
 * here. Each boards/<name>.c file defines strong implementations of
 * board_wifi_up(), board_nodes(), and board_set_log() (and any other
 * board-specific runtime helpers); init.c does not know which file
 * answers, only that the cmdline-named board does.
 *
 * The diagnostics sink is a function pointer rather than a direct call
 * to netlog(), because netlog() lives in init.c and the off-target
 * tests include init.c whole -- which would otherwise need to see
 * boards_biscuit.c too. board_set_log() wires it once at boot.
 *
 * boards_biscuit.c does NOT include this header; it declares the
 * prototypes locally. This is so its definitions are unambiguously
 * STRONG (the off-target tools get WEAK stubs from boards_stubs.h,
 * which boards_biscuit.c never sees). The two halves of the contract
 * would drift apart if they both lived in the same header. */
#ifndef EMOS_BOARDS_BOARDS_H
#define EMOS_BOARDS_BOARDS_H

#include <stddef.h>

/* What board_wifi_up logs to. Set by init.c to a single-argument sink
 * that appends a pre-formatted line to /run/net.log; the board code
 * formats with vsnprintf() before calling. A varargs function pointer
 * would be cleaner, but C has no portable way to forward a va_list,
 * and the double-format costs 200 bytes of stack and nothing else.
 *
 * NULL is harmless -- the board code checks before calling. */
typedef void (*board_log_fn)(const char *line);

/* The board chrdev table. Returned as a pointer + length so callers
 * iterate without depending on the table's storage class. */
struct board_node { const char *path; int major, minor; };

/* Prototypes for the runtime entry points. init.c sees these through
 * this header; boards_biscuit.c does NOT include this header and
 * declares them locally instead, so its definitions are unambiguously
 * STRONG and not re-declared weak. The weak fallbacks for the
 * off-target tests live in boards_stubs.h. */
void board_set_log(board_log_fn fn);
const struct board_node *board_nodes(size_t *count);
int board_wifi_up(const char *patch_dir);
int board_wifi_prepare(void);

/* Tell the kernel's LED driver to release its hold on the ring.
 *
 * The is31fl3236 driver used by both biscuit and radar runs an animation
 * out of its probe until userspace clears the `boot_animation` sysfs
 * attribute. Stock Android's init writes that zero on the first boot
 * (init.recovery.leds.rc -- one line, nothing else). We have to do the
 * same -- BEFORE the kernel's animation has a chance to start pulling
 * userspace traffic into a steady cadence.
 *
 * Called from init's main(), so a missing implementation is build-broken
 * not crash-broken. The stub in boards_stubs.h is silent, so the off-
 * target tools link without either boards file declaring it. */
void board_anim_stop(void);

#endif /* EMOS_BOARDS_BOARDS_H */
