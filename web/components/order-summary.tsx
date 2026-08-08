'use client'

import { money } from '@/components/format'

function Row({
  label,
  value,
  testId,
  strong = false,
}: {
  label: string
  value: string
  testId: string
  strong?: boolean
}) {
  return (
    <div className={`flex items-baseline justify-between py-1 ${strong ? 'text-base' : 'text-sm'}`}>
      <span className={strong ? 'font-semibold text-ink' : 'text-muted'}>{label}</span>
      <span data-testid={testId} className={strong ? 'font-semibold text-ink' : 'text-ink'}>
        {value}
      </span>
    </div>
  )
}

/**
 * Totals block. `totalCents` is rendered through `money`, which yields
 * "$NaN" when the caller hands it an amount that isn't there.
 */
export function OrderSummary({
  subtotalCents,
  discountCents,
  taxCents,
  totalCents,
  couponCode,
  prefix = 'summary',
}: {
  subtotalCents?: number | null
  discountCents?: number | null
  taxCents?: number | null
  totalCents?: number | null
  couponCode?: string | null
  prefix?: string
}) {
  const discount = Number(discountCents || 0)

  return (
    <div data-testid={`${prefix}-block`}>
      <Row label="Subtotal" value={money(subtotalCents ?? 0)} testId={`${prefix}-subtotal`} />
      {discount > 0 ? (
        <Row
          label={couponCode ? `Discount (${couponCode})` : 'Discount'}
          value={`-${money(discount)}`}
          testId={`${prefix}-discount`}
        />
      ) : null}
      <Row label="Tax" value={money(taxCents ?? 0)} testId={`${prefix}-tax`} />
      <div className="my-2 border-t border-line" />
      <Row label="Total" value={money(totalCents)} testId={`${prefix}-total`} strong />
    </div>
  )
}
