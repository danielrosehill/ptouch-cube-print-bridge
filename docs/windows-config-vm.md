# Configuring the printer from a headless Linux host

Some PT-P710BT settings — **auto power-off above all** — live in printer NVRAM and
are writable only by Brother's *Printer Setting Tool*, which is Windows/Mac and
USB-only. See [`hardware-notes.md`](hardware-notes.md) for why every lighter route
is a dead end (Bluetooth, the mobile apps, `ptouch-print`, and Wine — the tool needs
a kernel-mode driver, which Wine has no model for).

So on a headless Linux box the answer is an on-demand Windows VM with the printer
passed through. Built on residenceserver 2026-07-28; the VM is reusable for any other
vendor tool with the same Windows-only problem, so it is kept rather than discarded.

## Getting a Windows ISO headlessly

Microsoft's consumer download page generates **session-bound, ~24h-expiring** links
behind a JS flow, and only offers a direct ISO if the browser claims to be non-Windows.
Driving it with headless Playwright failed here — the page kept collapsing to
`about:blank` between calls. Don't bother.

**Use [UUP dump](https://uupdump.net) instead.** It pulls the same packages from
Microsoft's own servers and composes the ISO locally, entirely from the CLI:

```bash
apt install -y aria2 cabextract wimtools genisoimage chntpw   # chntpw is required
                                                              # and NOT obvious — the
                                                              # script aborts without it

# find the build (this is the *feature update* entry, not the cumulative update)
curl -s 'https://api.uupdump.net/listid.php?search=19045&sortByDate=1'   # Win10 22H2

UUID=<uuid from above>
curl -sL -o pkg.zip -d "autodl=2&updates=1&cleanup=1" \
  "https://uupdump.net/get.php?id=$UUID&pack=en-us&edition=professional"
unzip -oq pkg.zip && ./uup_download_linux.sh          # ~15 min, produces a 3.9 GB ISO
```

Note `response.builds` is a **dict keyed by uuid**, not a list — parsing it as a list
is the obvious first mistake. Pick the *"Feature update to Windows 10, version 22H2"*
row; the *"Cumulative Update"* row is a patch, not installation media.

## VM shape

Deliberately boring, and matched to the conventions already on the box (see the
`haos` / `unifi-os` domains):

| | | why |
|---|---|---|
| disk bus | **SATA**, not virtio | Windows has no in-box virtio disk driver; SATA avoids needing a virtio-win ISO at install time |
| NIC | **e1000e**, not virtio | same reason, and you *need* working network in the VM to fetch the Brother driver |
| firmware | BIOS, no TPM | Win10 needs neither; skip the UEFI+swtpm dance that Win11 forces |
| graphics | VNC on `127.0.0.1` | matches the other VMs; reach it by SSH tunnel, nothing exposed |
| autostart | **disabled** | normally off, brought up on demand |

```bash
virt-install --name win-utility --memory 4096 --vcpus 4 --cpu host-passthrough \
  --disk path=/var/lib/libvirt/images/win-utility.qcow2,size=64,format=qcow2,bus=sata \
  --cdrom /var/lib/libvirt/boot/uup-win10/19041.1_PROFESSIONAL_X64_EN-US.ISO \
  --network bridge=br0,model=e1000e \
  --graphics vnc,listen=127.0.0.1 --video qxl --os-variant win10 --noautoconsole
virsh autostart --disable win-utility
```

Windows installs unactivated — no key, works indefinitely, just a watermark and
locked personalisation settings. Fine for a config-tool VM, and unlike an Enterprise
*evaluation* image it does not expire and start hourly reboots after 90 days.

## Driving it

[`../scripts/win-utility.sh`](../scripts/win-utility.sh), installed on the server as
`/usr/local/bin/win-utility`:

```bash
win-utility up                # start
win-utility view              # prints the exact SSH tunnel + VNC command
win-utility printer-attach    # hand the printer to the VM
win-utility printer-detach    # give it back, restart the bridge
win-utility down              # graceful shutdown
```

## Setting auto power-off

1. `win-utility up`, then `win-utility view` and connect.
2. In Windows, install the PT-P710BT driver + **Printer Setting Tool** from Brother.
3. **Press the printer's power button**, then `win-utility printer-attach`.
   The printer must be awake — when it sleeps it leaves the USB bus entirely and
   there is nothing to pass through. You then have <60 min to finish, which is the
   whole joke of this exercise.
4. Printer Setting Tool → **Device Settings → Basic → Auto Power Off → None → Apply**.
   Set the Li-ion entry too if it appears separately. Nothing is written until Apply.
5. `win-utility printer-detach`, then `win-utility down`.
6. Confirm the host has it back: `curl -s localhost:9180/health`.

The setting is in printer NVRAM, so this is once and done — it survives reboots,
re-cabling, and host swaps.

## Gotchas

- **WMS label printing is down while the printer is attached to the VM.** The host
  loses the device outright. `printer-attach` stops `wms-print-bridge` and
  `printer-detach` restarts it, so the window is only as long as you take.
- Passthrough is matched by **vendor:product (`04f9:20af`), never port path** — same
  rule the bridge follows, so replugging into a different port doesn't break it.
- If `virsh attach-device` reports the device is busy, the bridge still holds it;
  `systemctl stop wms-print-bridge` and retry.
