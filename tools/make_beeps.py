"""Generates the M300 beep clips for the screen (stdlib only).

Same format as DGUS-reloaded's sounds: mono, 16-bit, 32 kHz PCM. Each clip is
under 0.5 s so it fits one 32 KB sound slot. Slots 10-17:

    10/11  500 Hz  short/long      12/13  1000 Hz short/long
    14/15 2000 Hz  short/long      16/17  3000 Hz short/long

Part of e5p-dgus. GPLv3.
"""
import math
import os
import struct
import sys
import wave

RATE = 32000
PITCHES = (500, 1000, 2000, 3000)
LENGTHS = (("short", 0.10), ("long", 0.45))
FIRST_ID = 10


def tone(freq, secs, amp=0.55, fade=0.006):
    n = int(RATE * secs)
    f = int(RATE * fade)
    out = bytearray()
    for i in range(n):
        env = min(1.0, i / f, (n - 1 - i) / f)   # no clicks at start/end
        out += struct.pack("<h", int(32767 * amp * env * math.sin(2 * math.pi * freq * i / RATE)))
    return bytes(out)


def main(out):
    os.makedirs(out, exist_ok=True)
    sid = FIRST_ID
    for freq in PITCHES:
        for name, secs in LENGTHS:
            path = os.path.join(out, "%d_beep_%d_%s.wav" % (sid, freq, name))
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(tone(freq, secs))
            print("wrote", os.path.basename(path), os.path.getsize(path), "bytes")
            sid += 1


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "screen_assets"))
