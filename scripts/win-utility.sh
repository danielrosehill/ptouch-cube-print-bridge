#!/usr/bin/env bash
# win-utility — drive the on-demand Windows VM on residenceserver (10.0.0.2).
#
# Exists because a few bits of hardware can only be configured by a vendor tool
# that is Windows-only. The PT-P710BT is the motivating case: its auto-power-off
# lives in printer NVRAM and is settable *only* from Brother's Printer Setting
# Tool over USB (see docs/hardware-notes.md). Wine cannot do it — that tool talks
# to the printer through a kernel-mode driver.
#
# The VM is normally OFF and does not autostart. Bring it up, do the job, bring
# it down. It costs nothing but disk while stopped.
#
# Run this ON residenceserver (or via: ssh residencehome-root win-utility <cmd>).
set -euo pipefail

VM=win-utility
VENDOR=0x04f9          # Brother Industries
PRODUCT=0x20af         # PT-P710BT
BRIDGE_SVC=wms-print-bridge.service

usage() {
    cat <<'EOF'
usage: win-utility <command>

  up                 start the VM
  down               graceful shutdown (falls back to nothing; use kill if stuck)
  kill               force off
  status             VM state, VNC port, whether the printer is attached
  view               print the SSH tunnel + viewer command to run on your workstation

  printer-attach     hand the PT-P710BT to the VM (stops the WMS print bridge)
  printer-detach     give it back to the host (restarts the WMS print bridge)

The printer must be POWERED ON before printer-attach — it drops off the USB bus
entirely when it sleeps, and there is nothing to pass through.
EOF
}

hostdev_xml() {
    cat <<EOF
<hostdev mode='subsystem' type='usb' managed='yes'>
  <source>
    <vendor id='$VENDOR'/>
    <product id='$PRODUCT'/>
  </source>
</hostdev>
EOF
}

printer_attached() {
    virsh dumpxml "$VM" 2>/dev/null | grep -q "vendor id='$VENDOR'"
}

printer_on_bus() {
    lsusb -d "${VENDOR#0x}:${PRODUCT#0x}" >/dev/null 2>&1
}

case "${1:-}" in
up)
    virsh start "$VM" 2>/dev/null || echo "already running"
    virsh domstate "$VM"
    ;;
down)
    virsh shutdown "$VM"
    echo "ACPI shutdown sent; Windows may take a minute. 'status' to check."
    ;;
kill)
    virsh destroy "$VM"
    ;;
status)
    echo "state:   $(virsh domstate "$VM" 2>/dev/null || echo 'not defined')"
    echo "vnc:     $(virsh vncdisplay "$VM" 2>/dev/null || echo '-')"
    echo "printer: $(printer_attached && echo 'attached to VM' || echo 'on host')"
    echo "on bus:  $(printer_on_bus && echo 'yes (powered on)' || echo 'NO - printer is asleep')"
    ;;
view)
    disp=$(virsh vncdisplay "$VM" 2>/dev/null || true)
    if [ -z "$disp" ]; then echo "VM is not running — 'win-utility up' first." >&2; exit 1; fi
    port=$((5900 + ${disp##*:}))
    cat <<EOF
VNC is bound to loopback on the server, so tunnel to it. On your workstation:

  ssh -L ${port}:127.0.0.1:${port} residencehome-root

then point a VNC client at  localhost:${port}
(e.g. remmina, vinagre, or:  vncviewer localhost:${port})
EOF
    ;;
printer-attach)
    if printer_attached; then echo "already attached"; exit 0; fi
    if ! printer_on_bus; then
        echo "ERROR: printer is not on the USB bus — press its power button first." >&2
        exit 1
    fi
    echo "stopping $BRIDGE_SVC so the host releases the device..."
    systemctl stop "$BRIDGE_SVC"
    hostdev_xml | virsh attach-device "$VM" /dev/stdin --live
    echo "attached. WMS label printing is DOWN until printer-detach."
    ;;
printer-detach)
    if printer_attached; then
        hostdev_xml | virsh detach-device "$VM" /dev/stdin --live || true
    fi
    echo "restarting $BRIDGE_SVC..."
    systemctl start "$BRIDGE_SVC"
    sleep 3
    systemctl is-active "$BRIDGE_SVC"
    ;;
*)
    usage
    exit 1
    ;;
esac
