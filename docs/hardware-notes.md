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

The printer switches itself off after idle and **won't wake over USB** — someone has
to press the power button. Design for it: the bridge's `/health` probe fails while the
printer is off, so the app can show "printer offline — is it powered on?" instead of a
dead Print button. (Powering the printer's USB from an always-on port helps; the
P710BT charges over USB but still auto-sleeps.)

## Status error codes seen in practice

| `--info` error | Meaning |
|---|---|
| `0x0000` | OK |
| `0x0100` | "Replace media" — in practice: job rejected (missing init sequence, see patch) |
| `0x0400` * | Communication error |
| `0x0001` * | No media / lid open |

\* per Brother's raster command reference status format.
