"""Цвета дорожек: в Live — номер в палитре из 70 цветов, в FL — любой RGB (байты R, G, B, 0)."""
import colorsys

LIVE_PALETTE = [
    0xFF94A6, 0xFFA529, 0xCC9927, 0xF7F47C, 0xBFFB00, 0x1AFF2F, 0x25FFA8, 0x5CFFE8, 0x8BC5FF, 0x5480E4,
    0x92A7FF, 0xD86CE4, 0xE553A0, 0xFFFFFF, 0xFF3636, 0xF66C03, 0x99724B, 0xFFF034, 0x87FF67, 0x3DC300,
    0x00BFAF, 0x19E9FF, 0x10A4EE, 0x007DC0, 0x886CE4, 0xB677C6, 0xFF39D4, 0xD0D0D0, 0xE2675A, 0xFFA374,
    0xD3AD71, 0xEDFFAE, 0xD2E498, 0xBAD074, 0x9BC48D, 0xD4FDE1, 0xCDF1F8, 0xB9C1E3, 0xCDBBE4, 0xAE98E5,
    0xE5DCE1, 0xA9A9A9, 0xC6928B, 0xB78256, 0x99836A, 0xBFBA69, 0xA6BE00, 0x7DB04D, 0x88C2BA, 0x9BB3C4,
    0x85A5C2, 0x8393CC, 0xA595B5, 0xBF9FBE, 0xBC7196, 0x7B7B7B, 0xAF3333, 0xA95131, 0x724F41, 0xDBC300,
    0x85961F, 0x539F31, 0x0A9C8E, 0x236384, 0x1A2F96, 0x2F52A2, 0x624BAD, 0xA34BAD, 0xCC2E6E, 0x3C3C3C,
]


def live_to_fl(index):
    """Номер цвета Live -> число цвета FL (R | G << 8 | B << 16) или None."""
    if not 0 <= index < len(LIVE_PALETTE):
        return None
    c = LIVE_PALETTE[index]
    return (c >> 16) | (c & 0xff00) | ((c & 0xff) << 16)


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(x / 255 for x in rgb))


def fl_to_live(color):
    """Цвет FL -> ближайший номер палитры Live. FL рисует дорожки темнее, поэтому сравниваем в первую
    очередь оттенок и насыщенность, яркость — слабее."""
    h, s, v = _hsv((color & 0xff, (color >> 8) & 0xff, (color >> 16) & 0xff))
    best, best_d = 0, None
    for i, c in enumerate(LIVE_PALETTE):
        h2, s2, v2 = _hsv((c >> 16, (c >> 8) & 0xff, c & 0xff))
        dh = min(abs(h - h2), 1 - abs(h - h2)) * min(s, s2) * 2
        d = dh * dh * 4 + (s - s2) ** 2 + (v - v2) ** 2 * 0.25
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best
