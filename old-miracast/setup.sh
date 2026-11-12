#!/bin/sh
# One-time setup for TV Cast. Run with:  sudo sh ~/Documents/tv-cast/setup.sh
#  - installs the "TV in charge" helper to /usr/local/sbin (owned by root)
#  - lets your user run only that helper as root without a password
#  - opens the firewall ports Miracast needs
set -e
[ "$(id -u)" = 0 ] || { echo "Please run with sudo"; exit 1; }
USER_NAME="${SUDO_USER:?run this with sudo from your normal account}"
SRC="$(dirname "$0")/tv-cast-helper"

install -o root -g root -m 0755 "$SRC" /usr/local/sbin/tv-cast-helper

RULE=/etc/sudoers.d/tv-cast
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/tv-cast-helper" > "$RULE.tmp"
chmod 0440 "$RULE.tmp"
visudo -cf "$RULE.tmp" >/dev/null && mv "$RULE.tmp" "$RULE"

if command -v ufw >/dev/null; then
    ufw allow 7236/tcp >/dev/null   # TV -> laptop control connection
    ufw allow 67/udp   >/dev/null   # TV asking the laptop for an address (normal mode)
fi
echo "TV Cast setup done."
