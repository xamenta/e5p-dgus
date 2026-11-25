"""Read, modify and write DWIN DGUS II layout files without DGUS Tools.

13_touch.bin    touch controls. Each record is a 16-byte header line
                (flag, page, x1 y1 x2 y2, pic_next, pic_on, 0xFE, type)
                followed by 0..n continuation lines that start with 0xFE.
                Popup-menu keys have header byte 14 = 0x00, byte 15 = key code.
                The file ends with FF FF.
14_variable.bin display variables. 16-byte header ("\\x14DGUS_2..."), an index
                of 4-byte entries per page (count LE16, offset BE16) up to 0x4000,
                then 32-byte records grouped by page, then one 32-byte FF block.

Round trip (parse -> build) reproduces DGUS Tools' output byte for byte; see
check_roundtrip(). Part of e5p-dgus. GPLv3.
"""
import struct

TOUCH_ENABLED = 0x08
T_RETURN_KEY = 0x05
DATA_START = 0x4000
REC = 32


def be16(v):
    return struct.pack(">H", v & 0xFFFF)


# ---------------------------------------------------------------------------
# 13 touch file
# ---------------------------------------------------------------------------
class TouchRecord:
    def __init__(self, lines):
        self.lines = [bytearray(l) for l in lines]

    @property
    def page(self):
        return self.lines[0][1]

    @property
    def box(self):
        h = self.lines[0]
        return tuple(struct.unpack(">4H", bytes(h[2:10])))

    @property
    def type(self):
        return self.lines[0][15]

    @property
    def vp(self):
        if len(self.lines) > 1:
            return struct.unpack(">H", bytes(self.lines[1][1:3]))[0]
        return None

    def to_bytes(self):
        return b"".join(bytes(l) for l in self.lines)

    def __repr__(self):
        vp = self.vp
        return "<Touch p%d %s t=%#x vp=%s>" % (self.page, self.box, self.type,
                                               hex(vp) if vp is not None else "-")


def return_key(page, box, vp, value, enabled=True, pic_on=0xFF00):
    """A button that writes `value` to `vp` (return key code control)."""
    x1, y1, x2, y2 = box
    head = bytes([TOUCH_ENABLED if enabled else 0, page]) + struct.pack(
        ">4HHH", x1, y1, x2, y2, 0xFF00, pic_on) + bytes([0xFE, T_RETURN_KEY])
    line = bytes([0xFE]) + be16(vp) + b"\x00" + be16(value) + b"\x00" * 10
    return TouchRecord([head, line])


def menu_key(page, box, keycode, pic_on):
    """A key inside a popup menu image page (returns keycode to the popup's VP)."""
    x1, y1, x2, y2 = box
    head = bytes([TOUCH_ENABLED, page]) + struct.pack(
        ">4HHH", x1, y1, x2, y2, 0xFF00, pic_on) + bytes([0x00, keycode])
    return TouchRecord([head])


class TouchFile:
    def __init__(self, data):
        if data[-2:] != b"\xff\xff" or (len(data) - 2) % 16:
            raise ValueError("unexpected 13 file layout")
        self.records = []
        for i in range(0, len(data) - 2, 16):
            line = data[i:i + 16]
            if line[0] == 0xFE:
                if not self.records:
                    raise ValueError("continuation line before first record")
                self.records[-1].lines.append(bytearray(line))
            else:
                self.records.append(TouchRecord([line]))

    def build(self):
        return b"".join(r.to_bytes() for r in self.records) + b"\xff\xff"

    def find(self, page=None, vp=None, type=None):
        return [r for r in self.records
                if (page is None or r.page == page)
                and (vp is None or r.vp == vp)
                and (type is None or r.type == type)]

    def remove(self, records):
        ids = {id(r) for r in records}
        self.records = [r for r in self.records if id(r) not in ids]

    def add(self, *records):
        self.records.extend(records)


# ---------------------------------------------------------------------------
# 14 display variable file
# ---------------------------------------------------------------------------
class VarRecord:
    def __init__(self, data):
        self.data = bytearray(data)
        assert len(self.data) == REC

    @property
    def type(self):
        return self.data[1]

    @property
    def vp(self):
        return struct.unpack(">H", bytes(self.data[6:8]))[0]

    def copy(self):
        return VarRecord(self.data)

    def __repr__(self):
        return "<Var t=%#x vp=%#x>" % (self.type, self.vp)


def text_var(template, vp, x, y, width, height, length, font=None, color=None):
    """Text display (type 0x11) based on an existing text record."""
    r = template.copy()
    d = r.data
    assert d[1] == 0x11, "template must be a text display"
    d[6:8] = be16(vp)
    d[8:10], d[10:12] = be16(x), be16(y)
    if color is not None:
        d[12:14] = be16(color)
    d[14:16], d[16:18] = be16(x), be16(y)
    d[18:20], d[20:22] = be16(x + width - 1), be16(y + height - 1)
    d[22:24] = be16(length)
    if font is not None:
        d[26] = d[27] = font
    return r


class VarFile:
    def __init__(self, data):
        if data[1:7] != b"DGUS_2":
            raise ValueError("not a DGUS II 14 file")
        self.header = bytes(data[:16])
        self.pages = {}
        self.index_len = DATA_START - 16
        end = None
        for p in range((DATA_START - 16) // 4):
            e = data[16 + p * 4:20 + p * 4]
            count = e[0] | (e[1] << 8)
            off = struct.unpack(">H", e[2:4])[0]
            if count:
                self.pages[p] = [VarRecord(data[off + k * REC:off + (k + 1) * REC])
                                 for k in range(count)]
            else:
                end = off if end is None else end
        self.trailer = bytes(data[-REC:])
        if self.trailer != b"\xff" * REC:
            raise ValueError("unexpected 14 file trailer")

    def build(self):
        out = bytearray(self.header)
        records = bytearray()
        # Every page's entry points at where its records start; an empty
        # page points at the running offset (where they would start).
        for p in range(self.index_len // 4):
            recs = self.pages.get(p) or []
            out += struct.pack("<H", len(recs)) + be16(DATA_START + len(records))
            for r in recs:
                records += r.data
        if DATA_START + len(records) + REC > 0x10000:
            raise ValueError("too many display variables")
        assert len(out) == DATA_START
        return bytes(out + records + self.trailer)

    def add(self, page, *records):
        self.pages.setdefault(page, []).extend(records)

    def find(self, page=None, vp=None):
        out = []
        for p, recs in self.pages.items():
            if page is not None and p != page:
                continue
            out += [r for r in recs if vp is None or r.vp == vp]
        return out


def check_roundtrip(touch_bytes, var_bytes):
    t = TouchFile(touch_bytes)
    v = VarFile(var_bytes)
    return t.build() == touch_bytes and v.build() == var_bytes
