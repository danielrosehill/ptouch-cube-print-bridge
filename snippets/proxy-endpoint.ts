/**
 * Server-side proxy between the HTTPS web app and the print bridge.
 *
 * Why a proxy at all? Browsers block HTTP calls from an HTTPS page (mixed
 * content) for every host except 127.0.0.1 — so the page can't reach the
 * bridge on the home LAN directly. Routing through the app server also means
 * print requests inherit the app's existing authentication, and printing
 * works from any device anywhere (phone on mobile data included).
 *
 * Shown in Nitro/h3 style (Nuxt server routes); the logic is three lines and
 * ports trivially to Express/Fastify/anything.
 *
 * PRINT_BRIDGE_URL should point at the bridge host's VPN address, e.g.
 *   PRINT_BRIDGE_URL=http://100.x.y.z:9180   (Tailscale)
 */
import { defineEventHandler, readBody, createError } from 'h3'

const BRIDGE_URL = process.env.PRINT_BRIDGE_URL || 'http://127.0.0.1:9180'

// GET /api/print/bridge-health — call when the print dialog opens; show the
// one-click Print button only when { available: true }.
export const bridgeHealth = defineEventHandler(async () => {
  try {
    const res = await $fetch<{ ok: boolean; printer?: string; tapeMm?: number | null }>(
      `${BRIDGE_URL}/health`,
      { signal: AbortSignal.timeout(4000) },
    )
    return { available: !!res?.ok, printer: res?.printer, tapeMm: res?.tapeMm }
  } catch {
    return { available: false }
  }
})

// POST /api/print/label — body: { imageDataUrl: 'data:image/png;base64,...', copies?: number }
export const printLabel = defineEventHandler(async (event) => {
  const body = await readBody<{ imageDataUrl?: string; copies?: number }>(event)
  if (!body?.imageDataUrl?.startsWith('data:image/png;base64,')) {
    throw createError({ statusCode: 400, statusMessage: 'imageDataUrl must be a PNG data URL' })
  }
  const copies = Math.min(20, Math.max(1, Math.round(Number(body.copies) || 1)))

  try {
    return await $fetch<{ ok: boolean; copies: number; tapeMm?: number }>(`${BRIDGE_URL}/print`, {
      method: 'POST',
      body: { imageDataUrl: body.imageDataUrl, copies },
      signal: AbortSignal.timeout(90_000), // multi-copy jobs are slow; don't cut them off
    })
  } catch (e: any) {
    throw createError({ statusCode: 502, statusMessage: e?.data?.error || 'Print bridge unreachable' })
  }
})
