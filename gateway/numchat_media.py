"""NumChat : emojis et images pour l'écran de la calculatrice.

- Les emojis d'un message sont remplacés par des caractères à usage privé
  (U+E000 + n) ; l'image 14×14 de chaque emoji part avant, dans une trame
  Emoji, et la calculatrice la dessine à la place du caractère.
- Les snaps et photos sont réduits pour tenir dans 320×240 et envoyés en
  RGB565, ligne par ligne. Rien n'est jamais écrit sur le disque.
"""
from __future__ import annotations

import io
import struct
from collections import OrderedDict

EMOJI_SIZE = 14
EMOJI_SLOTS = 32  # see ChatController::k_emojiSlots
RENDER_SIZE = 56
BACKGROUND = (16, 16, 16)  # message background on the calculator (0x101010)
SCREEN_W, SCREEN_H = 320, 240


# --- Découpage des emojis ----------------------------------------------------

def _is_regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_modifier(cp: int) -> bool:
    return (cp in (0xFE0F, 0xFE0E, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF
            or 0xE0020 <= cp <= 0xE007F)


def _is_pictographic(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
            or 0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF
            or cp in (0x3030, 0x303D, 0x3297, 0x3299))


def split_emoji(text: str) -> list[tuple[bool, str]]:
    """Découpe le texte en morceaux (est_emoji, texte). Un emoji composé
    (couleur de peau, drapeau, séquence ZWJ comme 👨‍👩‍👧) reste d'un seul tenant."""
    out: list[tuple[bool, str]] = []
    i, n = 0, len(text)
    plain = []
    while i < n:
        cp = ord(text[i])
        nxt = ord(text[i + 1]) if i + 1 < n else 0
        starts = (_is_pictographic(cp)
                  or (_is_regional(cp) and _is_regional(nxt))
                  or (nxt in (0xFE0F, 0x20E3) and cp != 0x20))
        if not starts:
            plain.append(text[i])
            i += 1
            continue
        j = i + 1
        if _is_regional(cp):
            j = i + 2
        while j < n:
            c = ord(text[j])
            if _is_modifier(c):
                j += 1
            elif c == 0x200D and j + 1 < n:
                j += 2
            else:
                break
        if plain:
            out.append((False, "".join(plain)))
            plain = []
        out.append((True, text[i:j]))
        i = j
    if plain:
        out.append((False, "".join(plain)))
    return out


# --- Images --------------------------------------------------------------------

def rgb565(image) -> bytes:
    """Image PIL RGB -> octets RGB565 petit-boutiste (format de KDColor)."""
    import numpy as np

    a = np.asarray(image.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


def emoji_from_rgba(rgba: bytes | None, size: int = RENDER_SIZE):
    """Rendu RGBA (carré de `size`) -> image 14×14 sur le fond des messages."""
    from PIL import Image

    tile = Image.new("RGBA", (EMOJI_SIZE, EMOJI_SIZE), BACKGROUND + (255,))
    if rgba:
        im = Image.frombytes("RGBA", (size, size), rgba)
        box = im.getbbox()
        if box:
            im = im.crop(box)
            im.thumbnail((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)
            tile.alpha_composite(im, ((EMOJI_SIZE - im.width) // 2, (EMOJI_SIZE - im.height) // 2))
    return tile.convert("RGB")


def render_emoji_pillow(emoji: str) -> bytes | None:
    """Rendu de secours sans navigateur (mode démo). Les emojis composés
    peuvent y perdre leur couleur de peau ou leur drapeau."""
    from PIL import Image, ImageDraw, ImageFont

    for path in ("/System/Library/Fonts/Apple Color Emoji.ttc",
                 "C:/Windows/Fonts/seguiemj.ttf",
                 "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"):
        try:
            font = ImageFont.truetype(path, 64 if "Apple" in path else 109)
            break
        except OSError:
            continue
    else:
        return None
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((0, 0), emoji[0], font=font, embedded_color=True)
    return canvas.resize((RENDER_SIZE, RENDER_SIZE)).tobytes()


class EmojiTable:
    """Emplacements d'emojis connus de la calculatrice (les plus anciens
    utilisés sont remplacés en premier)."""

    def __init__(self) -> None:
        self.slots: OrderedDict[str, int] = OrderedDict()

    def reset(self) -> None:
        self.slots.clear()

    def resolve(self, emojis: list[str], render, send) -> dict[str, int]:
        """Renvoie l'emplacement de chaque emoji ; ceux qui manquent sont rendus
        (render(liste) -> liste de RGBA) puis envoyés (send(slot, pixels))."""
        missing = [e for e in dict.fromkeys(emojis) if e not in self.slots]
        if missing:
            rendered = render(missing)
            for emoji, rgba in zip(missing, rendered):
                if len(self.slots) >= EMOJI_SLOTS:
                    _, slot = self.slots.popitem(last=False)
                else:
                    slot = len(self.slots)
                    used = set(self.slots.values())
                    slot = next(s for s in range(EMOJI_SLOTS) if s not in used)
                self.slots[emoji] = slot
                send(slot, rgb565(emoji_from_rgba(rgba)))
        for emoji in emojis:
            self.slots.move_to_end(emoji)
        return {e: self.slots[e] for e in emojis}


def emoji_payload(slot: int, pixels: bytes) -> bytes:
    return bytes([slot, EMOJI_SIZE, EMOJI_SIZE]) + pixels


def fit_picture(png: bytes, max_w: int = SCREEN_W, max_h: int = SCREEN_H):
    """PNG (en mémoire) -> image PIL RGB réduite pour tenir dans l'écran."""
    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((max_w, max_h), Image.BILINEAR)
    return im


def picture_rows(frame: int, image, rows_per_frame: int | None = None):
    """Découpe une image en trames MediaRows : u16 frame, u16 y, u8 rows,
    u8 last, puis les pixels RGB565."""
    data = rgb565(image)
    w, h = image.size
    step = rows_per_frame or max(1, 4000 // (w * 2))
    for y in range(0, h, step):
        rows = min(step, h - y)
        last = 1 if y + rows >= h else 0
        yield struct.pack("<HHBB", frame, y, rows, last) + data[y * w * 2:(y + rows) * w * 2]
