'use client'

import { useEffect, useState } from 'react'

/**
 * Quantity stepper. Emits a change only when the value actually settles on a
 * new positive integer, so one user interaction produces one request.
 */
export function QuantityInput({
  id,
  value,
  min = 1,
  max = 99,
  disabled = false,
  onChange,
}: {
  id: number | string
  value: number
  min?: number
  max?: number
  disabled?: boolean
  onChange: (qty: number) => void
}) {
  const [draft, setDraft] = useState(String(value))

  useEffect(() => {
    setDraft(String(value))
  }, [value])

  function clamp(n: number) {
    if (!Number.isFinite(n)) return value
    return Math.min(max, Math.max(min, Math.trunc(n)))
  }

  function commit(raw: string) {
    const next = clamp(Number(raw))
    setDraft(String(next))
    if (next !== value) onChange(next)
  }

  return (
    <div className="inline-flex items-stretch overflow-hidden rounded-md border border-line">
      <button
        type="button"
        data-testid={`qty-decrement-${id}`}
        aria-label="Decrease quantity"
        disabled={disabled || value <= min}
        onClick={() => onChange(clamp(value - 1))}
        className="px-2.5 text-sm text-ink disabled:opacity-40"
      >
        −
      </button>
      <input
        type="number"
        inputMode="numeric"
        data-testid={`qty-input-${id}`}
        aria-label="Quantity"
        value={draft}
        min={min}
        max={max}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        }}
        className="w-14 border-x border-line px-2 py-1.5 text-center text-sm outline-none"
      />
      <button
        type="button"
        data-testid={`qty-increment-${id}`}
        aria-label="Increase quantity"
        disabled={disabled || value >= max}
        onClick={() => onChange(clamp(value + 1))}
        className="px-2.5 text-sm text-ink disabled:opacity-40"
      >
        +
      </button>
    </div>
  )
}
