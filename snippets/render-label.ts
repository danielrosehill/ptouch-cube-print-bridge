/**
 * Browser-side label renderer (framework-free extract).
 *
 * Renders a QR code + big readable ID + optional item name to a <canvas> at
 * 12 px/mm. Show the canvas as the live preview in your print dialog — what
 * you see is byte-for-byte what prints. Send `canvas.toDataURL('image/png')`
 * to the bridge; it rescales to the loaded tape's printable height.
 *
 * The name block auto-fits: it tries the largest font whose fully-wrapped
 * lines fit the available space, shrinking down to a ~2mm legibility floor,
 * and only then falls back to an ellipsized last line. Long names stay
 * complete and readable instead of getting chopped.
 *
 * QR generation uses the `qrcode` npm package (any QR-to-canvas lib works).
 */
import QRCode from 'qrcode';

export interface TapePreset {
  id: string;
  label: string;
  /** Actual tape width in mm — display only */
  tapeMm: number;
  /** Rendered label layout in mm, landscape: width runs along the tape */
  widthMm: number;
  heightMm: number;
  /** Narrow tapes force ID-only (no room for a name line) */
  nameCapable: boolean;
}

// The tape width is the label's *height*: the label runs long along the tape.
export const TAPE_PRESETS: TapePreset[] = [
  { id: 'cube-24', label: 'P-Touch Cube — 24mm tape', tapeMm: 24, widthMm: 62, heightMm: 24, nameCapable: true },
  { id: 'cube-18', label: 'P-Touch Cube — 18mm tape', tapeMm: 18, widthMm: 58, heightMm: 18, nameCapable: false },
  { id: 'cube-12', label: 'P-Touch Cube — 12mm tape', tapeMm: 12, widthMm: 54, heightMm: 12, nameCapable: false },
];

const SCALE = 12; // px per mm (~305 dpi) for crisp downscaling to the 180 dpi head
const MIN_NAME_PX = 2 * SCALE; // ~2mm text on tape — smallest clearly legible size
const FONT_WEIGHT = 800;

export interface LabelSpec {
  preset: TapePreset;
  /** Short human-readable ID printed big, e.g. "A-0142" */
  readableId: string;
  /** Optional name line(s); auto-shrinks and wraps */
  name?: string;
  /** URL encoded into the QR code (e.g. a short link to the item page) */
  qrUrl: string;
}

export async function renderLabel(spec: LabelSpec): Promise<HTMLCanvasElement> {
  const qr = document.createElement('canvas');
  await QRCode.toCanvas(qr, spec.qrUrl, { margin: 0, errorCorrectionLevel: 'M', width: 480 });

  const w = Math.round(spec.preset.widthMm * SCALE);
  const h = Math.round(spec.preset.heightMm * SCALE);
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  drawLabelCell(canvas.getContext('2d')!, qr, spec, 0, 0, w, h);
  return canvas;
}

function drawLabelCell(
  ctx: CanvasRenderingContext2D,
  qr: HTMLCanvasElement,
  spec: LabelSpec,
  x: number, y: number, w: number, h: number,
) {
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = '#000000';
  ctx.textBaseline = 'middle';

  // QR left, square, padded
  const pad = Math.round(1.5 * SCALE);
  const qrSide = h - 2 * pad;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(qr, x + pad, y + pad, qrSide, qrSide);

  const tx = x + pad + qrSide + Math.round(2 * SCALE);
  const tw = x + w - pad - tx;
  if (tw <= 0) return;

  const name = (spec.name || '').trim();
  const showName = name.length > 0 && spec.preset.nameCapable;

  if (showName) {
    // ID on top, name below — left aligned, block vertically centred
    const idSize = fitFontSize(ctx, spec.readableId, tw, h * 0.45);
    const gap = Math.round(1.2 * SCALE);
    const idLineH = idSize;
    const nameMaxH = h - 2 * pad - idLineH - gap;
    const { size: nameSize, lines } = fitNameBlock(
      ctx, name, tw, nameMaxH, Math.max(MIN_NAME_PX, Math.round(idSize * 0.6)),
    );
    const nameLineH = nameSize * 1.15;
    const blockH = idLineH + gap + lines.length * nameLineH;
    let cy = y + h / 2 - blockH / 2 + idLineH / 2;

    ctx.textAlign = 'left';
    ctx.font = font(idSize);
    ctx.fillText(spec.readableId, tx, cy);
    cy += idLineH / 2 + gap;
    ctx.font = font(nameSize);
    for (const line of lines) {
      cy += nameLineH / 2;
      ctx.fillText(line, tx, cy);
      cy += nameLineH / 2;
    }
  } else {
    // ID only — centred both axes
    const size = fitFontSize(ctx, spec.readableId, tw, h - 2 * pad);
    ctx.textAlign = 'center';
    ctx.font = font(size);
    ctx.fillText(spec.readableId, tx + tw / 2, y + h / 2);
  }
}

const font = (size: number) => `${FONT_WEIGHT} ${size}px Arial, sans-serif`;

// Largest font (px) that fits `text` within maxW x maxH on one line
function fitFontSize(ctx: CanvasRenderingContext2D, text: string, maxW: number, maxH: number): number {
  let size = Math.floor(maxH);
  while (size > 6) {
    ctx.font = font(size);
    if (ctx.measureText(text).width <= maxW && size <= maxH) break;
    size -= 1;
  }
  return size;
}

// Largest name font whose fully-wrapped lines fit the box without truncation;
// falls back to the minimum legible size with an ellipsized last line.
function fitNameBlock(
  ctx: CanvasRenderingContext2D, text: string, maxW: number, maxH: number, maxSize: number,
): { size: number; lines: string[] } {
  for (let size = maxSize; size >= MIN_NAME_PX; size--) {
    const lines = wrapAll(ctx, text, maxW, size);
    if (lines && lines.length * size * 1.15 <= maxH) return { size, lines };
  }
  const maxLines = Math.max(1, Math.floor(maxH / (MIN_NAME_PX * 1.15)));
  return { size: MIN_NAME_PX, lines: wrapClamped(ctx, text, maxW, MIN_NAME_PX, maxLines) };
}

// Greedy wrap with no line cap; null if any single word is too wide at this size
function wrapAll(ctx: CanvasRenderingContext2D, text: string, maxW: number, size: number): string[] | null {
  ctx.font = font(size);
  const lines: string[] = [];
  let line = '';
  for (const word of text.split(/\s+/)) {
    if (ctx.measureText(word).width > maxW) return null;
    const test = line ? `${line} ${word}` : word;
    if (ctx.measureText(test).width > maxW) {
      lines.push(line);
      line = word;
    } else {
      line = test;
    }
  }
  if (line) lines.push(line);
  return lines;
}

// Wrap to at most maxLines, ellipsizing the last line if content remains
function wrapClamped(
  ctx: CanvasRenderingContext2D, text: string, maxW: number, size: number, maxLines: number,
): string[] {
  ctx.font = font(size);
  const lines: string[] = [];
  let line = '';
  for (const word of text.split(/\s+/)) {
    const test = line ? `${line} ${word}` : word;
    if (ctx.measureText(test).width > maxW && line) {
      lines.push(line);
      line = word;
      if (lines.length === maxLines - 1) break;
    } else {
      line = test;
    }
  }
  if (line && lines.length < maxLines) lines.push(line);
  if (lines.length === maxLines && lines.join(' ').length < text.length) {
    let last = lines[maxLines - 1];
    while (last && ctx.measureText(last + '…').width > maxW) last = last.slice(0, -1);
    lines[maxLines - 1] = last + '…';
  }
  return lines;
}
