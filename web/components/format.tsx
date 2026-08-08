'use client'

/**
 * Money formatting.
 *
 * Every amount in this app is integer cents. `money` never throws — an
 * amount that is not a number renders as "$NaN", which is exactly what the
 * customer sees when the API response no longer carries the field the UI
 * reads (BUG-003).
 */
export function money(cents: number | null | undefined): string {
  const n = cents === null || cents === undefined ? Number.NaN : Number(cents)
  if (!Number.isFinite(n)) return '$NaN'
  const negative = n < 0
  const value = (Math.abs(n) / 100).toFixed(2)
  return `${negative ? '-' : ''}$${value}`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

const COUPON_MESSAGES: Record<string, string> = {
  coupon_expired: 'This coupon has expired.',
  expired: 'This coupon has expired.',
  coupon_not_found: "We don't recognise that coupon code.",
  invalid_coupon: "We don't recognise that coupon code.",
  coupon_invalid: "We don't recognise that coupon code.",
  coupon_inactive: 'This coupon is no longer active.',
  coupon_exhausted: 'This coupon has reached its usage limit.',
  coupon_max_uses: 'This coupon has reached its usage limit.',
  coupon_min_subtotal: 'Your subtotal is too low for this coupon.',
  min_subtotal_not_met: 'Your subtotal is too low for this coupon.',
}

/** Turn the API's machine-readable coupon error into customer-facing copy. */
export function couponErrorMessage(code: string | undefined | null): string {
  const key = String(code || '').toLowerCase()
  return COUPON_MESSAGES[key] || 'This coupon could not be applied.'
}
