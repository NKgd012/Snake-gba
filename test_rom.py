#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless test-bench for snake.gba using Unicorn Engine.

Emulates the ARM7TDMI code of the ROM, fakes the two hardware registers the
game polls (VCOUNT and KEYINPUT), and checks the game logic.  Also renders the
GBA framebuffer to PNG so the result can be inspected visually.
"""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_R4, UC_ARM_REG_R5,
                               UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10)

import build_rom as B

ROM_BASE = 0x08000000
EWRAM = 0x02000000
IWRAM = 0x03000000
IO = 0x04000000
VRAM = 0x06000000

KEY_RIGHT = 0x10
KEY_LEFT = 0x20
KEY_UP = 0x40
KEY_DOWN = 0x80
KEY_START = 0x08

ALL_RELEASED = 0x3FF


class Emu:
    def __init__(self, rom, labels, autostart=True):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        for base, size in ((0x08000000, 0x40000),
                           (0x02000000, 0x40000),
                           (0x03000000, 0x8000),
                           (0x04000000, 0x1000),
                           (0x06000000, 0x18000)):
            self.uc.mem_map(base, size)
        self.uc.mem_write(0x08000000, rom)
        # VCOUNT advances by one scanline every time the game polls it, which
        # is enough to model the vblank sync (0..227, 160..227 = vblank)
        self.vcount = 0
        self.uc.mem_write(IO + 6, struct.pack('<H', 0))
        self.uc.mem_write(IO + 0x130, struct.pack('<H', ALL_RELEASED))
        self.frames = 0
        self.limit = 0
        self.key_fn = lambda f: ALL_RELEASED
        self.stopped = False
        self.l_main = labels['l_main']
        # the ROM boots to a title screen and then waits in READY; tap
        # START + a direction unless a test wants to inspect those screens
        self.boot_keys = ([ALL_RELEASED & ~KEY_START,
                           ALL_RELEASED & ~KEY_START,
                           ALL_RELEASED & ~KEY_RIGHT] if autostart else [])
        self.uc.hook_add(UC_HOOK_CODE, self._hook,
                         begin=labels['l_main'], end=labels['l_main'])
        for name in ('wv_low', 'wv_high'):
            self.uc.hook_add(UC_HOOK_CODE, self._tick_vcount,
                             begin=labels[name], end=labels[name])

    def _tick_vcount(self, uc, address, size, user_data):
        self.vcount = (self.vcount + 1) % 228
        uc.mem_write(IO + 6, struct.pack('<H', self.vcount))

    def _hook(self, uc, address, size, user_data):
        self.frames += 1
        if self.boot_keys:
            keys = self.boot_keys.pop(0)
        else:
            keys = self.key_fn(self.frames)
        uc.mem_write(IO + 0x130, struct.pack('<H', keys))
        if self.frames >= self.limit:
            uc.emu_stop()

    def run(self, n):
        self.limit = self.frames + n
        if self.frames == 0:
            start = 0x080000C0
        else:
            start = self.uc.reg_read(UC_ARM_REG_PC)
            if start == self.l_main:
                # we stopped on the frame hook; the body for this frame has
                # not run yet, so resume just after it
                start += 4
        self.uc.emu_start(start, 0xFFFFFFFF, timeout=0, count=0)

    # ---- inspection helpers ------------------------------------------------
    def u32(self, addr):
        return struct.unpack('<I', self.uc.mem_read(addr, 4))[0]

    def u8(self, addr):
        return self.uc.mem_read(addr, 1)[0]

    @property
    def score(self):
        return self.u32(B.V_SCORE)

    @property
    def state(self):
        return self.u32(B.V_STATE)

    @property
    def head(self):
        return self.uc.reg_read(UC_ARM_REG_R4), self.uc.reg_read(UC_ARM_REG_R5)

    @property
    def length(self):
        return self.uc.reg_read(UC_ARM_REG_R8)

    def occ_count(self):
        data = bytes(self.uc.mem_read(B.V_OCC, 540))
        return sum(1 for b in data if b)

    def food(self):
        return self.u32(B.V_FOOD)

    def set_food(self, cell):
        self.uc.mem_write(B.V_FOOD, struct.pack('<I', cell))

    def set_keys(self, fn):
        self.key_fn = fn

    def vram(self):
        return bytes(self.uc.mem_read(VRAM, 240 * 160 * 2))


def write_png(path, w, h, rgb):
    raw = b''.join(b'\x00' + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(raw, 9)) +
           chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)


def save_screenshot(emu, path, scale=3):
    vram = emu.vram()
    w, h = 240, 160
    out = bytearray()
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = struct.unpack_from('<H', vram, (y * w + x) * 2)[0]
            r = (v & 0x1F) * 255 // 31
            g = ((v >> 5) & 0x1F) * 255 // 31
            b = ((v >> 10) & 0x1F) * 255 // 31
            px = bytes((r, g, b))
            row += px * scale
        for _ in range(scale):
            out += row
    write_png(path, w * scale, h * scale, bytes(out))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok  " + msg)


def main():
    rom, info = B.build()
    labels = info['labels']

    print("[1] header")
    check(rom[0:4] == struct.pack('<I', 0xEA00002E), "ARM branch to 0x080000C0")
    check(rom[4:0xA0] == B.NINTENDO_LOGO, "official 156-byte Nintendo logo")
    check(rom[0xA0:0xA5] == b'SNAKE', "game title")
    chk = 0
    for b in rom[0xA0:0xBD]:
        chk -= b
    check(rom[0xBD] == (chk - 0x19) & 0xFF, "header complement check valid")

    print("[1b] title screen")
    emu = Emu(rom, labels, autostart=False)
    emu.run(1)
    check(emu.state == 2, "boot lands on the title screen")
    save_screenshot(emu, 'shot_title.png')
    emu.set_keys(lambda f: ALL_RELEASED & ~KEY_START if f <= 5 else ALL_RELEASED)
    emu.run(2)
    check(emu.state == 3, "START arms the game (READY)")
    save_screenshot(emu, 'shot_ready.png')
    emu.set_keys(lambda f: ALL_RELEASED & ~KEY_RIGHT if f <= 8 else ALL_RELEASED)
    emu.run(2)
    check(emu.state == 0, "first direction leaves READY")
    check(emu.head == (12, 10), "snake starts at 12,10")
    check(emu.length == 3, "snake starts with 3 segments")

    # idling in READY must never kill the snake (this is what made the game
    # feel broken: it used to drive straight into the wall at boot)
    idle = Emu(rom, labels, autostart=False)
    idle.run(1)
    idle.set_keys(lambda f: ALL_RELEASED & ~KEY_START if f <= 3 else ALL_RELEASED)
    idle.run(200)
    check(idle.state == 3, "still waiting in READY after 200 frames idle")
    check(idle.head == (12, 10), "snake does not move while idle in READY")

    print("[2] boot + movement")
    emu = Emu(rom, labels)
    emu.run(40)
    hx, hy = emu.head
    check(emu.state == 0, "game is running")
    check(hy == 10, "head stayed on row 10 (moving right)")
    check(hx >= 14, "head advanced right (x=%d after 40 frames)" % hx)
    check(emu.occ_count() == 3, "occupancy map holds exactly 3 cells")
    save_screenshot(emu, 'shot_play.png')

    print("[3] turning")
    emu = Emu(rom, labels)
    emu.set_keys(lambda f: ALL_RELEASED & ~KEY_UP if 5 <= f <= 12 else ALL_RELEASED)
    emu.run(60)
    hx, hy = emu.head
    check(hy < 10, "UP key steered the snake up (y=%d)" % hy)
    check(emu.state == 0, "still alive after the turn")

    print("[4] eating food")
    emu = Emu(rom, labels)
    emu.run(5)
    cell = (10 + 0) * 30 + 13          # one cell to the right of the head
    emu.set_food(cell)
    emu.run(20)
    check(emu.score == 1, "score went to 1 after eating")
    check(emu.length == 4, "snake grew to 4 segments")
    check(emu.occ_count() == 4, "occupancy map agrees (4 cells)")
    check(emu.food() != cell, "a new food cell was spawned")
    check(emu.u8(B.V_OCC + emu.food()) == 0, "food is not on the snake")

    print("[5] 5 meals -> speed up")
    emu = Emu(rom, labels)
    emu.run(5)
    interval0 = emu.u32(B.V_INTERVAL)
    # teleport food right in front of the snake, five times
    for i in range(5):
        hx, hy = emu.head
        emu.set_food(hy * 30 + hx + 1)
        emu.run(16)
    check(emu.score == 5, "score == 5")
    check(emu.u32(B.V_INTERVAL) == interval0 - 1,
          "interval dropped %d -> %d" % (interval0, emu.u32(B.V_INTERVAL)))

    print("[6] wall collision")
    emu = Emu(rom, labels)
    emu.run(400)
    check(emu.state == 1, "snake died at the right wall")
    save_screenshot(emu, 'shot_gameover.png')

    print("[7] START restarts")
    base = emu.frames
    emu.set_keys(lambda f: ALL_RELEASED & ~KEY_START if base + 2 <= f <= base + 4 else ALL_RELEASED)
    emu.run(4)
    check(emu.state == 3, "START restarts into READY")
    emu.set_keys(lambda f: ALL_RELEASED & ~KEY_RIGHT if f <= base + 8 else ALL_RELEASED)
    emu.run(2)
    check(emu.state == 0, "game restarted")
    check(emu.score == 0, "score reset")
    check(emu.head == (12, 10), "snake back at its start position")
    check(emu.length == 3, "length reset to 3")
    emu.set_keys(lambda f: ALL_RELEASED)
    emu.run(40)
    check(emu.state == 0 and emu.head[0] > 12, "play continues after restart")
    save_screenshot(emu, 'shot_restart.png')

    print("\nall tests passed")
    return rom, info


if __name__ == '__main__':
    main()


