#!/bin/bash
# e5p-dgus installer for Raspberry Pi (MainsailOS / Raspberry Pi OS)
# Usage (as your normal user, from this folder):   ./install.sh
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
HOME_DIR="$HOME"
CFG_DIR="$HOME_DIR/printer_data/config"
SERVICE=e5p-dgus

if [ "$(id -u)" = "0" ]; then
    echo "Run this as your normal user (e.g. pi), not root. It will sudo when needed."
    exit 1
fi

echo "== Installing dependencies (python3-serial, python3-jinja2)"
sudo apt-get update -qq
sudo apt-get install -y python3-serial python3-jinja2

echo "== Adding $USER_NAME to the dialout group (serial port access)"
sudo usermod -aG dialout "$USER_NAME"

echo "== Config"
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/e5p_dgus.cfg" ]; then
    cp "$SRC/e5p_dgus.cfg.example" "$CFG_DIR/e5p_dgus.cfg"
    echo "   created $CFG_DIR/e5p_dgus.cfg"
else
    echo "   keeping existing $CFG_DIR/e5p_dgus.cfg"
fi

echo "== systemd service"
sudo tee /etc/systemd/system/$SERVICE.service > /dev/null <<EOF
[Unit]
Description=e5p-dgus DWIN touchscreen for Klipper
After=network-online.target moonraker.service
Wants=moonraker.service

[Service]
Type=simple
User=$USER_NAME
SupplementaryGroups=dialout
WorkingDirectory=$SRC
ExecStart=/usr/bin/python3 -m e5p_dgus -c $CFG_DIR/e5p_dgus.cfg
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE

# Let Mainsail/Moonraker start/stop/restart the service
ASVC="$HOME_DIR/printer_data/moonraker.asvc"
if [ -f "$ASVC" ] && ! grep -qx "$SERVICE" "$ASVC"; then
    echo "$SERVICE" >> "$ASVC"
    echo "   added $SERVICE to moonraker.asvc (restart Moonraker to see it in Mainsail)"
fi

# Mainsail/Fluidd updates: register with Moonraker's update manager when this
# folder is a git clone (git clone https://github.com/xamenta/e5p-dgus.git)
MRCONF="$CFG_DIR/moonraker.conf"
if [ -d "$SRC/.git" ] && [ -f "$MRCONF" ] && ! grep -q "^\[update_manager $SERVICE\]" "$MRCONF"; then
    ORIGIN="$(git -C "$SRC" remote get-url origin 2>/dev/null || true)"
    BRANCH="$(git -C "$SRC" symbolic-ref --short HEAD 2>/dev/null || echo main)"
    if [ -n "$ORIGIN" ]; then
        read -r -p "Add e5p-dgus to Mainsail's update manager (moonraker.conf)? [Y/n] " ans
        if [ "${ans,,}" != "n" ]; then
            cp "$MRCONF" "$MRCONF.bak-e5p-dgus"
            {
                echo ""
                echo "[update_manager $SERVICE]"
                echo "type: git_repo"
                echo "path: $SRC"
                echo "origin: $ORIGIN"
                echo "primary_branch: $BRANCH"
                echo "managed_services: $SERVICE"
            } >> "$MRCONF"
            echo "   added [update_manager $SERVICE] (backup: moonraker.conf.bak-e5p-dgus)"
            sudo systemctl restart moonraker || true
        fi
    fi
fi

echo
echo "== UART setup"
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
NEED_REBOOT=0
read -r -p "Configure the Pi 4 UART for the screen (disable serial console, move Bluetooth off the UART)? [Y/n] " ans
if [ "${ans,,}" != "n" ]; then
    if command -v raspi-config > /dev/null; then
        sudo raspi-config nonint do_serial_cons 1   # login shell over serial: off
        sudo raspi-config nonint do_serial_hw 0     # serial hardware: on
    else
        echo "   raspi-config not found: disable the serial console and enable_uart=1 manually"
    fi
    if ! grep -q "^dtoverlay=disable-bt" "$BOOTCFG"; then
        echo "dtoverlay=disable-bt" | sudo tee -a "$BOOTCFG" > /dev/null
        echo "   added dtoverlay=disable-bt to $BOOTCFG"
    fi
    if ! grep -q "^enable_uart=1" "$BOOTCFG"; then
        echo "enable_uart=1" | sudo tee -a "$BOOTCFG" > /dev/null
    fi
    sudo systemctl disable hciuart 2> /dev/null || true
    NEED_REBOOT=1
fi

echo
if [ "$NEED_REBOOT" = "1" ]; then
    echo "Done. REBOOT the Pi now (sudo reboot); the screen service starts automatically."
else
    sudo systemctl restart $SERVICE
    echo "Done. Service started. Logs: journalctl -u $SERVICE -f   or   ~/printer_data/logs/e5p_dgus.log"
fi
