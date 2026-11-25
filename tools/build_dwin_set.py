"""Build the e5p-dgus v1.2 screen firmware from a DGUS-reloaded DWIN_SET.

    python3 build_dwin_set.py <base DWIN_SET> <output DWIN_SET>

<base> is the folder make_dwin_set.bat produces from DGUS-reloaded-Klipper
(13_touch.bin, 14_variable.bin, BMPs ...). The script copies it, replaces the
page images with ../screen_assets/*.bmp and patches the touch/display files:

  * Home: 4 tiles (Temperature, Move, Settings, Print) + STOP button
  * STOP button on Print status, Print adjust and Move
  * Heat presets popup: PLA / ABS / PETG / TPU
  * Shutdown page: Restart button (FIRMWARE_RESTART)
  * New pages 21 (file list) and 22 (start print)

Standard library only. Part of e5p-dgus. GPLv3.
"""
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dwin_layout as L  # noqa: E402

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screen_assets")

# Screen addresses used by e5p_dgus/screen/*.cfg (keep in sync)
VP_SWITCH_PAGE = 0x2000
VP_ESTOP = 0x2040
VP_RESTART = 0x2041
VP_FILE_ROW = 0x2042
VP_FILE_SCROLL = 0x2043
VP_PRINT_CONFIRM = 0x2044
VP_PRESET = 0x2010
VP_MESSAGE = 0x3000
VP_FILE_ROWS = 0x3200          # 5 x 0x20
VP_FILE_PAGE = 0x32A0
VP_CONFIRM_NAME = 0x3300       # 2 x 0x20
VP_CONFIRM_INFO = 0x3340       # 3 x 0x20

PAGE_HOME, PAGE_STATUS, PAGE_ADJUST, PAGE_TEMP, PAGE_MOVE = 1, 2, 3, 5, 8
PAGE_FILES, PAGE_CONFIRM = 21, 22
PAGE_PRESETS, PAGE_PRESETS_SEL, PAGE_KILL = 210, 211, 250

STOP_BOX = (398, 0, 479, 33)
LIGHT, BRIGHT, DIM = 0xDEFB, 0xEF7D, 0xBDAF


def popup_data(rec):
    """Data bytes of a multi-line touch record (without the FE markers)."""
    return bytearray(b"".join(bytes(l[1:]) for l in rec.lines[1:]))


def set_popup_data(rec, data):
    for i, l in enumerate(rec.lines[1:]):
        l[1:] = data[i * 15:(i + 1) * 15]


def patch_touch(t):
    # Home: 3 tiles -> 4 tiles
    t.remove([r for r in t.find(page=PAGE_HOME, vp=VP_SWITCH_PAGE) if r.box != (0, 0, 39, 39)])
    for x, target in zip((12, 128, 244, 360), (5, 8, 9, PAGE_FILES)):
        t.add(L.return_key(PAGE_HOME, (x, 76, x + 107, 183), VP_SWITCH_PAGE, target))

    for page in (PAGE_HOME, PAGE_STATUS, PAGE_ADJUST, PAGE_MOVE):
        t.add(L.return_key(page, STOP_BOX, VP_ESTOP, 1))

    # Heat presets popup on the temperature menu: wider menu area
    (popup,) = t.find(page=PAGE_TEMP, vp=VP_PRESET)
    d = popup_data(popup)
    assert struct.unpack(">H", d[3:5])[0] == PAGE_PRESETS
    d[5:13] = struct.pack(">4H", 95, 99, 384, 202)   # area cut from page 210
    d[13:17] = struct.pack(">2H", 95, 84)            # where it is shown
    set_popup_data(popup, d)
    t.remove(t.find(page=PAGE_PRESETS))
    t.add(L.menu_key(PAGE_PRESETS, (347, 98, 385, 136), 0xFF, 0xFF00))
    for i, x in enumerate((106, 174, 242, 310), 1):
        t.add(L.menu_key(PAGE_PRESETS, (x, 155, x + 63, 191), i, PAGE_PRESETS_SEL))

    t.add(L.return_key(PAGE_KILL, (175, 220, 304, 252), VP_RESTART, 1))

    # File list
    t.add(L.return_key(PAGE_FILES, (0, 0, 39, 39), VP_SWITCH_PAGE, PAGE_HOME))
    for i in range(5):
        y = 42 + i * 45
        t.add(L.return_key(PAGE_FILES, (8, y, 404, y + 40), VP_FILE_ROW, i))
    t.add(L.return_key(PAGE_FILES, (412, 42, 472, 138), VP_FILE_SCROLL, 0))
    t.add(L.return_key(PAGE_FILES, (412, 166, 472, 262), VP_FILE_SCROLL, 1))

    # Start print confirmation
    t.add(L.return_key(PAGE_CONFIRM, (0, 0, 39, 39), VP_SWITCH_PAGE, PAGE_FILES))
    t.add(L.return_key(PAGE_CONFIRM, (122, 216, 232, 252), VP_SWITCH_PAGE, PAGE_FILES))
    t.add(L.return_key(PAGE_CONFIRM, (248, 216, 358, 252), VP_PRINT_CONFIRM, 1))

    # keep DGUS Tools' ordering: by page (stable)
    t.records.sort(key=lambda r: r.page)


def patch_vars(v):
    # Header message shares the header with STOP: narrow it to x 112..399
    for page in (PAGE_HOME, PAGE_STATUS, PAGE_ADJUST, PAGE_MOVE):
        (msg,) = v.find(page=page, vp=VP_MESSAGE)
        msg.data[8:10] = msg.data[14:16] = L.be16(112)
        msg.data[18:20] = L.be16(399)

    (message,) = v.find(page=PAGE_TEMP, vp=VP_MESSAGE)
    text = v.find(page=PAGE_KILL)[0]   # 20 px text template
    v.pages[PAGE_FILES] = [message.copy()]
    for i in range(5):
        v.add(PAGE_FILES, L.text_var(text, VP_FILE_ROWS + i * 0x20, 18, 52 + i * 45,
                                     380, 22, 38, font=0x14, color=LIGHT))
    v.add(PAGE_FILES, L.text_var(text, VP_FILE_PAGE, 414, 144, 57, 18, 6, font=0x12, color=DIM))

    v.pages[PAGE_CONFIRM] = [message.copy()]
    for i in range(2):
        v.add(PAGE_CONFIRM, L.text_var(text, VP_CONFIRM_NAME + i * 0x20, 24, 52 + i * 26,
                                       440, 24, 40, font=0x16, color=BRIGHT))
    for i in range(3):
        v.add(PAGE_CONFIRM, L.text_var(text, VP_CONFIRM_INFO + i * 0x20, 24, 120 + i * 24,
                                       432, 20, 48, font=0x12, color=LIGHT))


def build(base, out):
    t_bytes = open(os.path.join(base, "13_touch.bin"), "rb").read()
    v_bytes = open(os.path.join(base, "14_variable.bin"), "rb").read()
    if not L.check_roundtrip(t_bytes, v_bytes):
        sys.exit("Base 13/14 files don't round-trip; refusing to patch")
    t, v = L.TouchFile(t_bytes), L.VarFile(v_bytes)
    if t.find(vp=VP_ESTOP):
        sys.exit("Base DWIN_SET is already patched; use the DGUS-reloaded one")
    patch_touch(t)
    patch_vars(v)

    if os.path.exists(out):
        sys.exit("%s already exists" % out)
    shutil.copytree(base, out)
    for name in sorted(os.listdir(ASSETS)):
        if name.lower().endswith(".bmp"):
            shutil.copy(os.path.join(ASSETS, name), os.path.join(out, name))
    with open(os.path.join(out, "13_touch.bin"), "wb") as f:
        f.write(t.build())
    with open(os.path.join(out, "14_variable.bin"), "wb") as f:
        f.write(v.build())
    print("Built %s: %d touch records, %d display variables"
          % (out, len(t.records), sum(len(x) for x in v.pages.values())))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2])
