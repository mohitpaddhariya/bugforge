'use client'

import { useState } from 'react'
import { useFlags } from '@/components/flags'

/**
 * Site-wide promo bar.
 *
 * The bar itself is a fixed strip at the top of the page. `.promo-layer` is
 * the transparent dismiss layer that sits over the strip so the whole bar is
 * a hit target; the visible ✕ lives in `.promo-shell`, which is stacked
 * above it.
 *
 * BUG-002 (`BUG_PROMO_OVERLAY`): with the flag on, the dismiss layer picks up
 * the wide-viewport treatment (`.promo-layer--wide`). See globals.css.
 */
export function PromoBanner() {
  const { flags } = useFlags()
  const [dismissed, setDismissed] = useState(false)

  if (dismissed) return null

  const wide = flags.BUG_PROMO_OVERLAY === true

  return (
    <>
      <div className="promo-shell" data-testid="promo-banner">
        <div className="flex h-[52px] items-center justify-center gap-3 overflow-hidden bg-ink px-4 text-center text-xs text-white sm:text-sm">
          <span className="truncate whitespace-nowrap">
            Free shipping this week — code{' '}
            <span className="font-semibold tracking-wide">WELCOME10</span> for $10 off
          </span>
          <button
            type="button"
            data-testid="promo-dismiss"
            aria-label="Dismiss promotion"
            onClick={() => setDismissed(true)}
            className="ml-2 shrink-0 rounded px-2 py-1 text-white/70 hover:text-white"
          >
            ✕
          </button>
        </div>
      </div>

      <div
        className={`promo-layer${wide ? ' promo-layer--wide' : ''}`}
        data-testid="promo-dismiss-layer"
        aria-hidden="true"
      />

      <div className="promo-spacer" />
    </>
  )
}
