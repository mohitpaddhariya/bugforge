'use client'

import { useState } from 'react'
import { applyCoupon, removeCoupon, normalizeError, type Cart } from '@/components/api-bridge'
import { couponErrorMessage } from '@/components/format'

/**
 * Coupon entry.
 *
 * This is BUG-005, the ticket that is not a bug: when the API rejects a code
 * with 400 {"error": "coupon_expired"} the customer is told, in plain words,
 * that the coupon has expired. That message must always be shown.
 */
export function CouponForm({
  appliedCode,
  onCart,
  disabled = false,
}: {
  appliedCode?: string | null
  onCart: (cart: Cart) => void
  disabled?: boolean
}) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onApply() {
    const trimmed = code.trim()
    setError(null)
    if (!trimmed) {
      setError('Enter a coupon code.')
      return
    }
    setBusy(true)
    try {
      const cart = await applyCoupon(trimmed.toUpperCase())
      onCart(cart)
      setCode('')
    } catch (err) {
      const e = normalizeError(err)
      setError(couponErrorMessage(e.code))
    } finally {
      setBusy(false)
    }
  }

  async function onRemove() {
    setError(null)
    setBusy(true)
    try {
      const cart = await removeCoupon()
      onCart(cart)
    } catch (err) {
      const e = normalizeError(err)
      setError(couponErrorMessage(e.code))
    } finally {
      setBusy(false)
    }
  }

  if (appliedCode) {
    return (
      <div className="card p-4">
        <p className="text-sm font-medium text-ink">Coupon</p>
        <div className="mt-2 flex items-center justify-between gap-3">
          <span
            data-testid="coupon-applied"
            className="rounded border border-green-200 bg-green-50 px-2 py-1 text-sm font-medium text-green-800"
          >
            {appliedCode} applied
          </span>
          <button
            type="button"
            data-testid="coupon-remove"
            disabled={busy || disabled}
            onClick={onRemove}
            className="text-sm text-muted underline hover:text-ink disabled:opacity-50"
          >
            Remove
          </button>
        </div>
        {error ? (
          <p data-testid="coupon-error" className="mt-2 text-sm text-red-600">
            {error}
          </p>
        ) : null}
      </div>
    )
  }

  return (
    <div className="card p-4">
      <label htmlFor="coupon-input" className="text-sm font-medium text-ink">
        Coupon code
      </label>
      <div className="mt-2 flex items-start gap-2">
        <div className="flex-1">
          <input
            id="coupon-input"
            data-testid="coupon-input"
            className="field"
            placeholder="e.g. WELCOME10"
            value={code}
            disabled={disabled}
            onChange={(e) => {
              setCode(e.target.value)
              if (error) setError(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                onApply()
              }
            }}
          />
          {error ? (
            <p data-testid="coupon-error" className="mt-2 text-sm text-red-600">
              {error}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          data-testid="coupon-apply"
          disabled={busy || disabled}
          onClick={onApply}
          className="btn btn-secondary"
        >
          {busy ? <span className="spinner" aria-hidden="true" /> : 'Apply'}
        </button>
      </div>
    </div>
  )
}
