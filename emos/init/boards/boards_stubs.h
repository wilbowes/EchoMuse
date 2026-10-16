/*
 * Weak stubs for the board-runtime interface.
 *
 * The off-target tools (cmdlinecheck, ringsim, pwcheck, tmoutcheck,
 * wpacheck, serialcheck) all #include init.c whole WITHOUT linking
 * boards_*.c. Their tests do not reach the wifi-up or device-node
 * code paths, so the linker still needs to resolve the references
 * init.c makes to board_nodes(), board_wifi_up(), and
 * board_set_log().
 *
 * The strong definitions come from boards_biscuit.c in the real
 * firmware build; in the off-target tests, these weak stubs win by
 * default and return safe no-ops. The stubs are never CALLED in the
 * off-target tests -- their tests exit before reaching the wifi-up
 * stage -- so a NULL pointer for the node table is harmless.
 *
 * Included only by init.c. boards_biscuit.c never sees this header, so
 * its strong definitions are not re-declared weak in the same TU and
 * there is no attribute mismatch at link time.
 *
 * The build system passes these symbols to the linker last; if
 * boards_biscuit.c is in the link, its strong versions take
 * precedence over these weak ones.
 */
#ifndef EMOS_BOARDS_BOARDS_STUBS_H
#define EMOS_BOARDS_BOARDS_STUBS_H

__attribute__((weak)) const struct board_node *board_nodes(size_t *count)
{
    if (count) *count = 0;
    return 0;
}

__attribute__((weak)) int board_wifi_up(const char *patch_dir)
{
    (void)patch_dir;
    return -1;
}

__attribute__((weak)) int board_wifi_prepare(void) { return 0; }

__attribute__((weak)) void board_set_log(board_log_fn fn) { (void)fn; }

/* No-op stub for the off-target tools. See the matching comment in
 * boards.h for why init.c calls this in main() and what it is meant
 * to stop. */
__attribute__((weak)) void board_anim_stop(void) { }

#endif /* EMOS_BOARDS_BOARDS_STUBS_H */
