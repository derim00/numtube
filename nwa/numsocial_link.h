/* NumSocial link for external apps (.nwa).
 *
 * The NumSocial firmware publishes its USB link to NumSocial on the computer
 * in the table external apps already use for eadk_display_draw_string: entry
 * 1 is the "NSOC" magic word, then the functions below. On any other
 * firmware the magic is missing: numsocial_available() says so and nothing
 * must be called.
 *
 * Frames are TMIA frames (type + payload), the same as NumTube, NumChat and
 * NumScreen use. This header is MIT licensed so that any app can use it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

extern char _userland_trampoline_address;

#define NUMSOCIAL_MAGIC 0x434F534Eu /* "NSOC" */

static inline uint32_t *numsocial_table(void) {
  return (uint32_t *)&_userland_trampoline_address;
}

static inline bool numsocial_available(void) {
  return numsocial_table()[1] == NUMSOCIAL_MAGIC;
}

/* Bytes of memory the link needs (lend it 8-aligned to numsocial_open). */
static inline unsigned numsocial_storage_size(void) {
  return ((unsigned (*)(void))numsocial_table()[2])();
}

/* Waits for NumSocial on the computer (Back cancels). 1 when connected. */
static inline int numsocial_open(void *storage, unsigned size) {
  return ((int (*)(void *, unsigned))numsocial_table()[3])(storage, size);
}

static inline int numsocial_send(uint8_t type, const void *payload, uint16_t length) {
  return ((int (*)(uint8_t, const void *, uint16_t))numsocial_table()[4])(type, payload, length);
}

/* Length of the frame copied into buffer, or -1 when none is ready. */
static inline int numsocial_poll(uint8_t *type, void *buffer, uint16_t capacity) {
  return ((int (*)(uint8_t *, void *, uint16_t))numsocial_table()[5])(type, buffer, capacity);
}

static inline void numsocial_close(void) { ((void (*)(void))numsocial_table()[6])(); }

static inline bool numsocial_plugged(void) {
  return ((int (*)(void))numsocial_table()[7])() != 0;
}
