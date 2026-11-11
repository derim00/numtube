/* Checks the NumSocial link from an external app: connects, sends a Log
 * line and pings the gateway, shows what comes back. Back quits. */
#include <eadk.h>
#include <stdio.h>
#include <string.h>

#include "../numsocial_link.h"

const char eadk_app_name[] __attribute__((section(".rodata.eadk_app_name"))) = "LinkTest";
const uint32_t eadk_api_level __attribute__((section(".rodata.eadk_api_level"))) = 0;

static uint64_t storage[1200];  // 9.6 KB, more than the link needs
static uint8_t buffer[512];

static void line(int y, const char *text, eadk_color_t color) {
  eadk_display_push_rect_uniform((eadk_rect_t){0, (uint16_t)y, 320, 20}, eadk_color_white);
  eadk_display_draw_string(text, (eadk_point_t){8, (uint16_t)y}, true, color, eadk_color_white);
}

int main(int argc, char *argv[]) {
  char text[64];
  eadk_display_push_rect_uniform(eadk_screen_rect, eadk_color_white);
  line(10, "NumSocial LinkTest", eadk_color_blue);
  if (!numsocial_available()) {
    line(40, "Firmware sans NumSocial", eadk_color_red);
  } else {
    snprintf(text, sizeof(text), "lien: %u octets", numsocial_storage_size());
    line(40, text, eadk_color_black);
    line(70, "Connexion... (Retour: annuler)", eadk_color_black);
    if (!numsocial_open(storage, sizeof(storage))) {
      line(70, "Pas de connexion", eadk_color_red);
    } else {
      line(70, "Connecte a NumSocial !", eadk_color_green);
      const char *hello = "linktest: bonjour depuis une appli externe";
      numsocial_send(0x4B, hello, (uint16_t)strlen(hello));
      int frames = 0;
      uint64_t lastPing = 0;
      while (!eadk_keyboard_key_down(eadk_keyboard_scan(), eadk_key_back)) {
        uint8_t type;
        int n;
        while ((n = numsocial_poll(&type, buffer, sizeof(buffer))) >= 0) {
          frames++;
          snprintf(text, sizeof(text), "trames: %d (derniere 0x%02X, %d o)", frames, type, n);
          line(100, text, eadk_color_black);
        }
        uint64_t now = eadk_timing_millis();
        if (now - lastPing > 1000) {
          lastPing = now;
          numsocial_send(0x0A, NULL, 0);  // Hello: the gateway answers HelloAck
        }
      }
      numsocial_close();
    }
  }
  line(200, "Retour : quitter", eadk_color_black);
  while (eadk_keyboard_key_down(eadk_keyboard_scan(), eadk_key_back)) {}
  while (!eadk_keyboard_key_down(eadk_keyboard_scan(), eadk_key_back)) {}
  return 0;
}
