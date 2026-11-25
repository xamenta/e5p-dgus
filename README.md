# e5p-dgus: stock Ender 5 Plus touchscreen on Klipper

This is a service that runs on the Raspberry Pi 4. It drives the Ender 5 Plus's
stock DWIN touchscreen (480x272 T5UID1) with the **DGUS-reloaded** screen
firmware. It gets printer state from **Moonraker** and sends G-code through
Moonraker, so you keep **mainline Klipper**. There's no forked Klipper and nothing
extra to flash on the mainboard.

It's a port of Desuuuu's `klippy/extras/t5uid1` (from his Klipper fork) from
inside Klipper out to the Pi. The page, variable and routine definitions in
`e5p_dgus/screen/` are his DGUS-reloaded files with a few fixes, so they match
the `DWIN_SET` built from `DGUS-reloaded-Klipper-master`.

```
 Screen (DWIN T5UID1) ──UART──► Pi 4 (e5p-dgus) ──HTTP──► Moonraker ──► Klipper ──USB──► Creality V4.2.2 board
```

## What's improved over the original

| | Original (Klipper fork) | e5p-dgus |
|---|---|---|
| Klipper | Old fork, never updated | Mainline. Update freely from Mainsail |
| Print start/end on screen | Needed `DGUS_PRINT_START`/`DGUS_PRINT_END` macros in your slicer G-code | Detected automatically from `print_stats`, including prints started from Mainsail |
| Print time | Wall clock minus pauses, lost on restart | Klipper's own `print_duration` |
| Z offset → **Save** | `SAVE_CONFIG` silently dropped the Z offset (`SET_GCODE_OFFSET` is temporary) | Runs `Z_OFFSET_APPLY_PROBE` first, so the offset is kept (configurable) |
| Bed mesh screen | Only showed the first 5x5 corner of larger meshes | 7x7, 9x9, … meshes are resampled to the 5x5 display |
| Probing progress icons | Klipper internals | Read from Klipper's console, scaled to any mesh size |
| Manual leveling "center" point | Wrong when `position_min` ≠ 0 | Fixed |
| G-code errors (e.g. "Must home axis first") | Only in the console | Shown in the screen's message bar |
| Klipper shutdown / not connected / Moonraker down | Shutdown only | All shown on the error page with the reason; screen recovers by itself |
| Service restart during a print | Back to the home screen | Goes straight to the print status page |
| PID values after `PID_CALIBRATE` | Old values until restart | Shows pending (unsaved) values |
| Volume/brightness | Written into printer.cfg via `SAVE_CONFIG` | Kept in a small state file. No Klipper restart |
| Temperature presets | Hard-coded | Set in `e5p_dgus.cfg` (PLA/ABS/PETG) |
| Updates | n/a | **v1.1:** Moonraker pushes changes over its websocket (~0.1 s to the screen); only changed values are sent to the screen; page self-check every 30 s. Falls back to HTTP polling automatically |

### New in v1.2 (needs the v1.2 screen firmware, see below)

- **Print from the screen:** a 4th *Print* tile on the home page opens a file
  list (newest first, 5 per page). Tap a file to see its print time, filament,
  layer height and temperatures from the slicer, then **Print**.
- **STOP button** (emergency stop) on Home, Print status, Adjust and Move.
  Press twice within 3 s (`estop_confirm: false` for a single press). It goes
  straight to Moonraker's emergency stop, skipping the G-code queue.
- **Restart button** on the error page: `FIRMWARE_RESTART`, or restarts the
  Klipper service when Klipper isn't connected.
- **TPU** heat preset (`tpu_hotend` / `tpu_bed`).

### New in v1.3: beeps

`M300 S<Hz> P<ms>` from your slicer or macros now beeps on the screen. The
installer copies `e5p_dgus_macros.cfg` to your config folder and offers to add
`[include e5p_dgus_macros.cfg]` to printer.cfg (restart Klipper after).
The screen firmware has 4 pitches (500, 1000, 2000, 3000 Hz), each short
(0.1 s) or long (0.45 s); M300 picks the closest, and beeps over 0.6 s
repeat the long one (up to ~3 s). `SCREEN_SOUND SOUND=<n> [VOLUME=<0-100>]`
plays any sound on the screen (1 boot, 2 click, 3 notification, 10-17 beeps).

Examples: end G-code `M300 S1000 P500`; in a macro
`M300 S2000 P100` then `M300 S3000 P100` for a two-tone chirp.

If you already have a `[gcode_macro M300]` (e.g. for a board beeper), the
installer leaves it alone; replace its body with the M300 from
`e5p_dgus_macros.cfg` to beep on the screen.

## Screen firmware (v1.2+)

The new buttons and pages are added by `tools/build_dwin_set.py`, which
patches the DGUS-reloaded `DWIN_SET` directly (no DGUS Tools needed) and adds
the images in `screen_assets/`:

```
python3 tools/build_dwin_set.py <DGUS-reloaded DWIN_SET> <new DWIN_SET>
```

Flash the result the same way as before (FAT32, 4096-byte allocation, under
8 GB). The v1.2 service works with the old screen firmware too, minus the new
buttons; the new screen firmware needs the v1.2 service.
`tools/make_screen_images.py` regenerates the images (needs Pillow).

---

## Step 1: Build the screen firmware (Windows PC)

The DGUS-reloaded project ships without the three generated `.bin` files.
You make them once in DWIN's free **DGUS Tools** (V7.x, from dwin-global.com).
The project was saved with V7.3.6.

1. Open `DGUS-reloaded-Klipper-master\project\DWprj.hmi` in DGUS Tools.
2. Click **Generate**. This writes `13*.bin`, `14*.bin` and `22*.bin` into `project\DWIN_SET`.
3. Run `e5p-dgus\tools\make_dwin_set.bat`. It renames the `.bin` files, checks
   they're all there, and builds `Ender5Screen\DWIN_SET_FLASH\DWIN_SET`. That
   folder uses the **flash-reset** `T5UID1.CFG`, which wipes Creality's old
   pages first. You need it when coming from stock.

## Step 2: Flash the screen

1. Get a microSD card **under 8 GB**. Format it **FAT32 with 4096-byte allocation**:
   `format X: /FS:FAT32 /A:4096`
2. Copy the `DWIN_SET` folder (the folder itself) to the card's root.
3. Power the printer off and unplug it. Open the screen housing. The SD slot is on the screen's own board.
4. Insert the card and power on. The screen turns blue and flashes. **Don't cut power**
   until it shows **END**.
5. Power off, remove the card, close the housing.

To go back to stock: flash Creality's `DWIN_SET`
(`Ender-5 Plus-Marlin2.0.8.3_HW_V4.2.2_SW_V1.0.7\DWIN_SET`) the same way, and plug
the screen back into the mainboard.

## Step 3: Wire the screen to the Pi

Unplug the screen cable from the Creality mainboard. Klipper doesn't use it.
Then wire the screen's connector to the Pi's 40-pin header:

| Screen pin | Signal | Pi 4 header pin |
|---|---|---|
| 1 | +5V | 2 or 4 (5V) |
| 2 | – | not connected |
| 3 | RX (screen receives) | 8 (GPIO14 / TXD) |
| 4 | TX (screen sends) | 10 (GPIO15 / RXD) |
| 5 | – | not connected |
| 6 | GND | 6 (GND) |

⚠ **Check the TX level first.** The Pi's GPIO is 3.3V only. With the screen
powered, measure screen pin 4 against GND. **~3.3V** means wire it directly.
**~5V** means put a divider on that wire: screen TX → 1 kΩ → Pi pin 10, and Pi
pin 10 → 2 kΩ → GND. Confirm the pin numbers on your cable with a meter before
connecting. Screen revisions differ.

## Step 4: Install on the Pi

Copy the `e5p-dgus` folder to the Pi, e.g. with WinSCP to `/home/pi/e5p-dgus`. Then:

```bash
cd ~/e5p-dgus
chmod +x install.sh
./install.sh        # answer Y to the UART question
sudo reboot
```

The installer:
- installs `python3-serial` and `python3-jinja2` from apt
- adds you to the `dialout` group
- creates `~/printer_data/config/e5p_dgus.cfg`, which you can edit from Mainsail
- installs and enables the `e5p-dgus` systemd service, and lists it in
  `moonraker.asvc` so Mainsail can restart it
- turns off the serial login console, enables the UART and adds
  `dtoverlay=disable-bt`, so `/dev/serial0` is the full hardware UART

Your printer.cfg needs `[display_status]`, `[pause_resume]` and
`[virtual_sdcard]`. Mainsail's `mainsail.cfg` already includes them. For the
leveling screens you also need `[probe]` (or `[bltouch]`) and `[bed_mesh]`.

## Checking it works

- On boot the screen shows the Klipper logo, then the home page with live temperatures.
- Logs: `~/printer_data/logs/e5p_dgus.log` (visible in Mainsail) or `journalctl -u e5p-dgus -f`
- More detail: set `log_level: DEBUG` in `e5p_dgus.cfg` and restart the service.

| Symptom | Likely cause |
|---|---|
| Screen stuck on its boot logo, log says nothing from the screen | TX/RX swapped, or the UART isn't set up (check `ls -l /dev/serial0` → `ttyAMA0`) |
| `Cannot open /dev/serial0` | Not rebooted after install, or not in `dialout` |
| Screen shows "Moonraker not reachable" | Moonraker down, or wrong `moonraker_url` |
| Touches do nothing | Screen TX → Pi RX wire, or the level divider |

## Updates from Mainsail (GitHub)

Once this folder is on GitHub, install it on the Pi with `git clone` instead
of copying files:

```bash
sudo systemctl stop e5p-dgus
mv ~/e5p-dgus ~/e5p-dgus.old            # the copied version, if any
git clone https://github.com/xamenta/e5p-dgus.git ~/e5p-dgus
cd ~/e5p-dgus && chmod +x install.sh && ./install.sh
```

The installer sees the git clone and offers to add this to `moonraker.conf`
(it keeps a backup):

```ini
[update_manager e5p-dgus]
type: git_repo
path: ~/e5p-dgus
origin: https://github.com/xamenta/e5p-dgus.git
primary_branch: main
managed_services: e5p-dgus
```

After that, new versions show up in Mainsail under **Machine > Update Manager**
next to Klipper and Moonraker, and updating restarts the screen service.
Use a **public** repo: Moonraker can't pull from a private one without a
token stored on the Pi.

## Updating from v1.0

Copy the new `e5p-dgus` folder over the old one on the Pi, then
`sudo systemctl restart e5p-dgus`. No reinstall needed; nothing new to install.
New options (`connection`, `min_update_interval`, `resync_interval`) have
sensible defaults; see `e5p_dgus.cfg.example` if you want to change them.
The log shows `Subscribed to N printer objects` when the websocket is in use.

## Files

```
e5p-dgus/
  e5p_dgus/            the service (python3 -m e5p_dgus)
    runtime.py         screen logic (port of t5uid1.py)
    dwin.py            DWIN serial protocol
    live.py            Moonraker websocket link (push updates, v1.1)
    websocket.py       small stdlib websocket client
    moonraker.py       Moonraker HTTP client (G-code, polling fallback)
    screen/*.cfg       DGUS-reloaded page/var/routine definitions (patched)
  screen_assets/       new/changed page images and beep sounds for the screen firmware
  e5p_dgus.cfg.example
  install.sh
  tools/make_dwin_set.bat     renames DGUS Tools output into a DWIN_SET
  tools/build_dwin_set.py     builds screen firmware v1.2 from that DWIN_SET
  tools/dwin_layout.py        reads/writes DWIN 13/14 layout files
  tools/make_screen_images.py draws screen_assets/*.bmp
  tools/make_beeps.py         generates screen_assets/*.wav (M300 beeps)
  e5p_dgus_macros.cfg         Klipper macros: M300, SCREEN_SOUND
  tests/               offline unit tests + end-to-end test with a fake screen
```

Run the tests: `python3 -m unittest discover -s tests` and `python3 tests/integration_test.py` (Linux).

License: GPLv3, same as DGUS-reloaded and Klipper. The original work is © Desuuuu.

Shoutout to Desuuuu : https://github.com/Desuuuu/DGUS-reloaded-Klipper
                      https://github.com/Desuuuu/klipper
