# PT-P710BT hardware notes (learned the hard way)

## USB, not Bluetooth

The PT-P710BT is marketed as a Bluetooth printer, but it has a USB-C-era micro-USB port
and shows up as a normal USB printer (`04f9:20af`). Driving it over USB from an
always-on Linux box is dramatically more reliable than maintaining a Bluetooth Classic
RFCOMM link from a headless server (pairing, binding, range, and reconnect issues all
disappear). Note the smaller PT-P300BT is Bluetooth-only — this architecture needs the
P710BT (24mm, USB) or P910BT (36mm, USB).

## The silent-failure bug (why the patch exists)

Stock `ptouch-print` lists the PT-P710BT as supported, but its device entry is missing
`FLAG_P700_INIT`. Symptom: `ptouch-print --info` works fine, print jobs exit 0, but
**nothing prints** — the LED flashes red and `--info` afterwards shows
`error = 0x0100` (the "replace media" bit in Brother's status format). The printer
stays in that error state until power-cycled and refuses subsequent jobs.

The P710BT shares its print engine with the PT-P700/P750W, which both carry
`FLAG_P700_INIT`. Adding the flag (see `patches/`) fixes it completely.

## The blank leader and `--precut`

The print head sits ~25mm behind the cutter, so every job mechanically feeds ~25mm of
blank tape before the printed area. Options:

- **`--precut` (recommended for on-demand printing):** the printer feeds the leader and
  cuts it off as a separate scrap piece, so the label itself comes out clean-edged.
  This is what Brother's own app does. Costs the 25mm as scrap but needs no scissors.
- **`--chain`:** skip the final feed/cut; the printed label stays inside and is pushed
  out by the *next* job. Near-zero waste, ideal for batch runs, but awkward for
  one-at-a-time printing (your label doesn't come out until the next print).
- **Default (neither):** label ejects with the 25mm leader attached — you cut it off
  by hand. Avoid.

The bridge uses `--precut` on every copy.

## Print geometry

- 180 dpi head, **128 dots** maximum printable height (on 24mm tape; narrower tapes
  expose fewer dots — `ptouch-print --info` reports the current tape's maximum).
- Labels are therefore landscape strips: height = across the tape (≤128 px at print
  resolution), width = along the tape, arbitrary length.
- Render client-side at higher resolution (12 px/mm ≈ 305 dpi) and let the bridge
  downscale to the reported max — text and QR codes stay crisp.
- 1-bit black/white only. The bridge thresholds at ~63% grey.

## Auto power-off

**60 minutes from the last print job — not from the last USB activity.** Measured
2026-07-28 from a bridge journal, three independent power-on windows, all with a
status read (`ESC i S`, the `--info` query) running every 300 s throughout:

| Window | Powered off after |
|---|---|
| last print 00:15:07 → off 01:15:52 | 60 m 45 s |
| last print 14:52:11 → off 15:52:44 | 60 m 33 s |
| powered on, **zero** print jobs | ~55–60 m |

That third row is the one that matters: **status reads do not count as activity and
do not defer the timer.** Only printing resets it. 60 min is also Brother's factory
default, which corroborates the measurement. Don't bother writing a status-poll
"keep-alive" — it cannot work, and it looks like it's doing something when it isn't.

It **won't wake over USB** either. `lsusb -v` reports the printer as bus-powered with
no remote-wakeup bit (`bmAttributes 0x80`), and when it powers off it leaves the USB
bus entirely — there is no endpoint left to poke. Someone has to press the button.
Charging it from an always-on port does not help; the P710BT charges over USB and
still auto-sleeps.

### The actual fix

Set **Auto Power Off = None** in Brother's *Printer Setting Tool → Device Settings →
Basic*. The selectable values are `None / 10 / 20 / 30 / 40 / 50 / 60` minutes, and
the choice is stored in the printer's own NVRAM — a one-time change that survives
reboots, re-cabling and firmware-agnostic host swaps.

The catch is that the tool is **Windows/Mac and USB-only**. Confirmed dead ends:

- Bluetooth cannot carry device settings, by design.
- The Design&Print / iPrint&Label mobile apps do not expose the setting.
- `ptouch-print` has no device-settings support at all (not in its option list).
- Wine is not a workaround: the tool reaches the printer through Brother's
  *kernel-mode* USB driver, and Wine implements Win32 user space only — it has no
  kernel driver model, so the tool launches and sees no printer.

On a headless Linux host the practical route is a throwaway/on-demand Windows VM with
the printer passed through by vendor:product id (`04f9:20af`), which is what
[`docs/windows-config-vm.md`](windows-config-vm.md) covers.

Until it's set, design for the power-off: the bridge's `/health` probe fails while the
printer is off, so the app can show "printer offline — is it powered on?" instead of a
dead Print button.

## Status error codes seen in practice

| `--info` error | Meaning |
|---|---|
| `0x0000` | OK |
| `0x0100` | "Replace media" — in practice: job rejected (missing init sequence, see patch) |
| `0x0400` * | Communication error |
| `0x0001` * | No media / lid open |

\* per Brother's raster command reference status format.
