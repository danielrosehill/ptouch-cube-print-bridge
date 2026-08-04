# Should the bridge go through CUPS?

Short answer: **no.** The bridge stays the sole owner of the USB device. This note
records why, so the question does not get re-litigated from scratch.

Verified 2026-07-30 on `residencehome` (Ubuntu resolute, PT-P710BT on USB
`04f9:20af`).

## The appeal of the idea

The bridge is reachable only over HTTP on `:9180`, and its API is label-shaped
(heading / subtext / QR). Nothing else on the machine can print to the label
printer — no `lp`, no GTK print dialog, no IPP from a laptop. A CUPS queue would
give all of that for free. That is a real gap, so the idea deserves a real answer.

## A CUPS driver for this printer does exist

`printer-driver-ptouch` 1.7.1 is in Ubuntu `resolute/main`, and it covers the
PT-P710BT (support landed upstream in v1.7).

It ships **no static PPDs**. Instead `/usr/lib/cups/driver/ptouch` is a Python
PPD *generator* that CUPS invokes:

```console
$ ./usr/lib/cups/driver/ptouch list | grep P710
"ptouch:0/ppd/ptouch-driver/Brother-PT-P710BT-ptouch-pt.ppd" en "Brother" \
  "Brother PT-P710BT Foomatic/ptouch-pt (recommended)" \
  "MFG:Brother;MDL:PT-P710BT;CMD:PT-CBP;DRV:Dptouch-pt,R1,M0,TF;"
```

The `MDL:PT-P710BT` in that IEEE-1284 string matches what the printer reports over
USB, so CUPS auto-discovery would bind the right PPD without hand-holding. The
print path itself would be the `rastertoptch` filter.

So the driver is not the obstacle.

## Obstacle 1: CUPS on this box is a snap

`residencehome` runs the OpenPrinting **snap** (`cups` 2.4.19-2, rev 1229), not
the deb. That rules out both ways of extending it:

- It ships no `usr/share/ppd/` and no `usr/share/cups/model/` directory at all, so
  an apt-installed PPD generator is invisible to it.
- Its filesystem is read-only squashfs, so a custom backend cannot be dropped into
  `/usr/lib/cups/backend/` either.
- It keeps its own confined config tree at `/var/snap/cups/common/etc/cups/`.

This is by design: the snap targets driverless IPP and "Printer Application"
snaps, not classic PPD/Foomatic drivers. **Any CUPS plan here starts with
replacing the snap with deb `cups`.**

Note also that the snap currently does nothing — `printers.conf` contains only
`NextPrinterId 2`, no container mounts its socket, and nothing but `cupsd` itself
binds `/var/snap/cups/common/run/cups.sock`. It is removable dead weight.

## Obstacle 2: only one process can own the device

`ptouch-print` claims the USB interface through libusb, detaching the kernel
driver. A CUPS `usb://` queue wants that same interface. **Both cannot be live at
once**, so this was never "add CUPS alongside the bridge" — it is a choice about
who owns the device.

## Why handing the device to CUPS would be a downgrade

`bridge/bridge.py` is not a thin wrapper around `ptouch-print`. It carries:

- tape-width autodetect read from the printer's status block, with rescaling
- `--precut` leader trimming (see [`hardware-notes.md`](hardware-notes.md))
- `PRINT_LOCK` serialisation of all jobs
- settle/verify timing around the physical print, not just the raster stream
- the `/health` reachability probe

The Foomatic path replaces the first of those with a static `PageSize` from a PPD
and provides none of the rest. Moving to CUPS for standardisation's sake would
trade a proven, device-specific path for a generic one that is worse at this
device. The gap it closes (occasional printing from other apps) is smaller than
the regression it introduces.

## If a generic queue is wanted later, invert the design

Keep the bridge as sole device owner and make CUPS a *client* of it:

```
other apps ──▶ CUPS queue ──▶ backend script ──▶ POST :9180/print ──▶ ptouch-print
label clients ─────────────────────────────────▶ POST :9180/print ──┘
```

A backend in `/usr/lib/cups/backend/` rasterises the incoming job and POSTs it.
One device owner, so no libusb contention and no cross-process locking to invent —
`PRINT_LOCK` already serialises everything, and every job keeps tape autodetect and
leader trimming.

Prerequisites: deb `cups` (the snap cannot host a backend), and a bridge endpoint
that accepts an arbitrary image rather than the label-shaped payload `/print`
takes today.

## Related: auto power-off is not reachable this way either

Worth stating because it is the usual reason to go looking for a better driver.
Brother's *Raster Command Reference* for the PT-E550W / P750W / P710BT family
defines 15 commands, and the only device-level knobs are:

| Command | Controls |
| --- | --- |
| `ESC i M` | auto-cut (bit 6), mirror printing (bit 7) |
| `ESC i K` | half-cut (unused on P710BT), no-chain, special tape, high-resolution, no-buffer-clear |
| `ESC i d` | margin / feed amount |

Grepping that reference for `auto.?power|power.?off|sleep|standby` returns zero
hits. Auto power-off is not an undocumented corner of the protocol — it is outside
it, living in the USB-only vendor channel the Windows Printer Setting Tool uses,
for which Brother publishes no reference. Brother's developer command-reference
index lists this family as Raster-only and does not list the P710BT at all. No
CUPS driver, and no fork of `ptouch-print`, can reach it. See
[`windows-config-vm.md`](windows-config-vm.md) for the route that does work.

Two features *are* in-protocol but unexposed by `ptouch-print`, if ever wanted:
mirror printing (`ESC i M` bit 7) and high-resolution mode (`ESC i K` bit 6).

## Deployment: run from a checkout

Until 2026-07-30 the deployed service ran a loose copy of `bridge.py` in a directory
with no git remote. It silently drifted 247 lines ahead of this repo — the keepalive
probe, `PRINT_LOCK` and the settle/verify timing all existed only on the host, while
the docs commits of 28 Jul described behaviour whose code was never committed.

The fix is structural rather than procedural: **the service executes the file inside a
clone of this repo**, so the running code and the tracked code cannot diverge.

```ini
ExecStart=/usr/bin/python3 /home/youruser/ptouch-cube-print-bridge/bridge/bridge.py
```

Deploying a change is then `git pull && systemctl restart <unit>`, and
`git -C <clone> status --porcelain` answers "is the host running anything unpushed?"
at a glance. On `residencehome` the unit is `wms-print-bridge.service` and the clone
is at `/home/daniel/ptouch-cube-print-bridge`; the retired directory was kept as
`wms-print-bridge.superseded-20260730` rather than deleted.

The host authenticates to GitHub over HTTPS through git's credential store, so pulls
need no interactive login. Nothing about that belongs in this repo — no token, no
`.env`. Note that an SSH *deploy* key is scoped to a single repository and greets you
as `owner/repo` rather than as your account, which is a confusing way to discover that
a host cannot push to anything else.

## Confirmed vs inferred

Confirmed by direct check: the PPD generator's model list and IEEE-1284 string;
the snap's missing PPD directories and confined config tree; nothing else using
the snap's socket; the command inventory in Brother's reference.

Inferred, not measured: the exact size of the output regression on the Foomatic
path. The reasoning above rests on which features the two paths have, not on a
print-quality comparison.
