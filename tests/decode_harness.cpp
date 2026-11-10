// Runs the calculator's tile decoder on the desktop.
//
// stdin: records of [u8 kind]
//   kind 1: u16 start_tile, u32 length, ops      -> decode into the picture
//   kind 2:                                      -> write the picture (raw
//                                                   RGB565 LE, 320x180) to stdout
// Build: c++ -std=c++17 -O2 -I../firmware/epsilon/apps/numtube decode_harness.cpp
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vector>

#include "tile_decoder.h"

using namespace Numtube;

struct Screen {
  uint16_t pixels[k_videoWidth * k_videoHeight] = {};
  void push(int x, int y, int width, const uint16_t* source) {
    if (x < 0 || y < 0 || y >= k_videoHeight || x + width > k_videoWidth) {
      fprintf(stderr, "out of bounds push %d %d %d\n", x, y, width);
      exit(2);
    }
    memcpy(pixels + y * k_videoWidth + x, source, width * 2);
  }
};

static bool readExact(void* out, size_t n) {
  return fread(out, 1, n, stdin) == n;
}

int main() {
  static Screen screen;
  static uint16_t row[TileDecoder<Screen>::k_rowBufferPixels];
  TileDecoder<Screen> decoder(row, &screen);
  std::vector<uint8_t> ops;
  uint8_t kind;
  while (readExact(&kind, 1)) {
    if (kind == 1) {
      uint8_t header[6];
      if (!readExact(header, 6)) return 1;
      uint16_t start = header[0] | (header[1] << 8);
      uint32_t length = header[2] | (header[3] << 8) | (header[4] << 16) |
                        (static_cast<uint32_t>(header[5]) << 24);
      ops.resize(length);
      if (length && !readExact(ops.data(), length)) return 1;
      decoder.decode(start, ops.data(), length);
    } else if (kind == 2) {
      fwrite(screen.pixels, 2, k_videoWidth * k_videoHeight, stdout);
      fflush(stdout);
    } else {
      return 1;
    }
  }
  return 0;
}
