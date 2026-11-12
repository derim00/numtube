/* NumSocial link for external apps (.nwa).
 *
 * The NumSocial firmware publishes its USB link to NumSocial on the computer in
 * a table found by its signature ("NUMSOCIAL-LINK1"), in the firmware's flash,
 * between the firmware's start and the external apps (where this app is). On
 * any other firmware there is none: numsocial_available() says so and nothing
 * must be called.
 *
 * Frames are TMIA frames (type + payload), the same as NumTube, NumChat and
 * NumScreen use. This header is MIT licensed so that any app can use it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

extern char _userland_trampoline_address;   /* (in the firmware: where to start looking) */

typedef struct {
  uint32_t magic[4];   /* "NUMSOCIAL-LINK1\0" */
  uint32_t version, count;
  void *functions[7];
} numsocial_table_t;

static const numsocial_table_t *numsocial_found;
static bool numsocial_looked;

/* Finds the table once (a few milliseconds). The signature is checked word by word against numbers,
 * not a string: this app's own copy of it would be found otherwise. */
static inline const numsocial_table_t *numsocial_table(void) {
  if (numsocial_looked) return numsocial_found;
  numsocial_looked = true;
  const uint32_t *p = (const uint32_t *)(((uintptr_t)&_userland_trampoline_address) & ~(uintptr_t)15);
  const uint32_t *end = (const uint32_t *)(uintptr_t)&numsocial_table;   /* (this app, after the firmware) */
  if ((uintptr_t)end <= (uintptr_t)p || (uintptr_t)end - (uintptr_t)p > 0x400000) end = p + 0x400000 / 4;
  for (; p + 12 < end; p += 4) {
    if (p[0] == 0x534D554Eu && p[1] == 0x4149434Fu && p[2] == 0x494C2D4Cu && p[3] == 0x00314B4Eu) {
      const numsocial_table_t *t = (const numsocial_table_t *)p;
      if (t->version == 1 && t->count >= 6) numsocial_found = t;
      break;
    }
  }
  return numsocial_found;
}

static inline bool numsocial_available(void) { return numsocial_table() != 0; }

/* Bytes of memory the link needs (lend it 8-aligned to numsocial_open). */
static inline unsigned numsocial_storage_size(void) {
  return ((unsigned (*)(void))numsocial_table()->functions[0])();
}

/* Waits for NumSocial on the computer (Back cancels). 1 when connected. */
static inline int numsocial_open(void *storage, unsigned size) {
  return ((int (*)(void *, unsigned))numsocial_table()->functions[1])(storage, size);
}

static inline int numsocial_send(uint8_t type, const void *payload, uint16_t length) {
  return ((int (*)(uint8_t, const void *, uint16_t))numsocial_table()->functions[2])(type, payload, length);
}

/* Length of the frame copied into buffer, or -1 when none is ready. */
static inline int numsocial_poll(uint8_t *type, void *buffer, uint16_t capacity) {
  return ((int (*)(uint8_t *, void *, uint16_t))numsocial_table()->functions[3])(type, buffer, capacity);
}

static inline void numsocial_close(void) { ((void (*)(void))numsocial_table()->functions[4])(); }

static inline bool numsocial_plugged(void) {
  return ((int (*)(void))numsocial_table()->functions[5])() != 0;
}
