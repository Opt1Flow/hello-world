#!/usr/bin/env bash
# Pre-flight checks + Pi-hole install for the Linux VM.
# Usage: sudo bash install-pihole.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "== Network =="
ip -4 -brief addr show scope global
echo "Default route: $(ip route show default | head -n1)"
echo
echo "Make sure the address above is on the SAME subnet as your router/LAN."
echo "If it starts with 10.0.2.x the VM is on NAT -- switch it to Bridged first."
echo

echo "== Internet check =="
if ! curl -fsS --max-time 10 -o /dev/null https://install.pi-hole.net; then
  echo "Cannot reach install.pi-hole.net -- fix networking first." >&2
  exit 1
fi
echo "OK"
echo

echo "== Port 53 check =="
if ss -lntup | grep -qE ':53\s'; then
  echo "Something is already listening on port 53:"
  ss -lntup | grep -E ':53\s' || true
  if systemctl is-active --quiet systemd-resolved; then
    echo "systemd-resolved stub detected (common on Ubuntu); Pi-hole's installer handles this."
  fi
else
  echo "Port 53 is free."
fi
echo

read -rp "Install Pi-hole now? [y/N] " answer
[[ ${answer,,} == y* ]] || exit 0

curl -sSL https://install.pi-hole.net | bash

echo
echo "Set the web admin password:"
pihole setpassword

ip4=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -n1)
echo
echo "Done. Admin page: http://${ip4}/admin"
echo "Next: reserve ${ip4} for this VM in your router, then set router DNS -> ${ip4}"
