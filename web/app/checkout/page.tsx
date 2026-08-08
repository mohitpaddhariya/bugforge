'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useMemo, useState } from 'react'
import {
  checkout,
  getCart,
  getMe,
  normalizeError,
  type Cart,
  type CartItem,
} from '@/components/api-bridge'
import { useFlags } from '@/components/flags'
import { OrderSummary } from '@/components/order-summary'
import { money } from '@/components/format'
import { Alert, Button, EmptyState, Field, LoadingBlock, PageHeading } from '@/components/ui'

function itemName(item: CartItem): string {
  return item.name || item.product?.name || `Item #${item.product_id}`
}

function lineTotal(item: CartItem): number {
  if (typeof item.line_total_cents === 'number') return item.line_total_cents
  return Number(item.unit_price_cents) * Number(item.qty)
}

export default function CheckoutPage() {
  const router = useRouter()
  const { flags } = useFlags()

  const [cart, setCart] = useState<Cart | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [name, setName] = useState('')
  const [line1, setLine1] = useState('221B Baker Street')
  const [city, setCity] = useState('Bengaluru')
  const [postal, setPostal] = useState('560001')
  const [country, setCountry] = useState('India')

  useEffect(() => {
    let alive = true

    Promise.all([getCart(), getMe().catch(() => null)])
      .then(([c, user]) => {
        if (!alive) return
        setCart(c)
        if (user?.name) setName(user.name)
        else if (user?.email) setName(user.email.split('@')[0])
      })
      .catch((err) => {
        const e = normalizeError(err)
        if (!alive) return
        if (e.status === 401) {
          router.push('/login?next=/checkout')
          return
        }
        setError(`We couldn't load your checkout (${e.code}).`)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })

    return () => {
      alive = false
    }
  }, [router])

  const items = cart?.items ?? []
  const computedSubtotal = useMemo(
    () => items.reduce((sum, item) => sum + lineTotal(item), 0),
    [items]
  )

  const subtotal = cart?.subtotal_cents ?? computedSubtotal
  const discount = Number(cart?.discount_cents || 0)
  const tax = Number(cart?.tax_cents || 0)
  const total = cart?.total_cents ?? subtotal - discount + tax

  function payload() {
    return {
      shipping_address: {
        name,
        line1,
        city,
        postal_code: postal,
        country,
      },
    }
  }

  async function placeOrder() {
    setError(null)
    setSubmitting(true)

    // Fixes #1042 (secondary). This used to have a branch that awaited the
    // request without a catch, so a non-2xx response left `submitting` true
    // forever: the button stayed grey and spinning with no error shown. That
    // is why the ticket said "spins" rather than "error", and why the customer
    // clicked Place Order three more times.
    try {
      const order = await checkout(payload())
      router.push(`/orders/${order.id}`)
    } catch (err) {
      const e = normalizeError(err)
      if (e.status === 401) {
        router.push('/login?next=/checkout')
        return
      }
      setError(
        e.status === 400
          ? `We couldn't place your order (${e.code}). Please review your cart and try again.`
          : "Something went wrong placing your order. Nothing was charged — please try again."
      )
      setSubmitting(false)
    }
  }

  if (loading) return <LoadingBlock label="Loading checkout…" />

  if (items.length === 0) {
    return (
      <div data-testid="checkout-page">
        <PageHeading title="Checkout" />
        <EmptyState testId="checkout-empty" title="There is nothing to check out.">
          <Link href="/" data-testid="checkout-continue-shopping" className="text-accent underline">
            Browse products
          </Link>
        </EmptyState>
      </div>
    )
  }

  return (
    <div data-testid="checkout-page">
      <PageHeading title="Checkout" subtitle="Review your details and place your order." />

      <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
        <section className="card p-5">
          <h2 className="text-sm font-semibold text-ink">Shipping address</h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Field
              label="Full name"
              data-testid="address-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="sm:col-span-2"
            />
            <Field
              label="Address"
              data-testid="address-line1"
              value={line1}
              onChange={(e) => setLine1(e.target.value)}
              className="sm:col-span-2"
            />
            <Field
              label="City"
              data-testid="address-city"
              value={city}
              onChange={(e) => setCity(e.target.value)}
            />
            <Field
              label="Postal code"
              data-testid="address-postal"
              value={postal}
              onChange={(e) => setPostal(e.target.value)}
            />
            <Field
              label="Country"
              data-testid="address-country"
              value={country}
              onChange={(e) => setCountry(e.target.value)}
              className="sm:col-span-2"
            />
          </div>

          <h2 className="mt-8 text-sm font-semibold text-ink">Items</h2>
          <ul data-testid="checkout-items" className="mt-3 divide-y divide-line border-y border-line">
            {items.map((item) => (
              <li
                key={item.id}
                data-testid={`checkout-item-${item.id}`}
                className="flex items-center justify-between gap-4 py-3 text-sm"
              >
                <span className="min-w-0 truncate">
                  {itemName(item)} <span className="text-muted">× {item.qty}</span>
                </span>
                <span className="shrink-0 font-medium">{money(lineTotal(item))}</span>
              </li>
            ))}
          </ul>
        </section>

        <aside>
          <div className="card p-4">
            <h2 className="mb-3 text-sm font-semibold text-ink">Order summary</h2>
            <OrderSummary
              prefix="checkout-summary"
              subtotalCents={subtotal}
              discountCents={discount}
              taxCents={tax}
              totalCents={total}
              couponCode={cart?.coupon_code}
            />

            {error ? (
              <div className="mt-4">
                <Alert testId="checkout-error">{error}</Alert>
              </div>
            ) : null}

            <div className="checkout-actions mt-4">
              <Button
                data-testid="place-order"
                onClick={() => {
                  placeOrder()
                }}
                loading={submitting}
                loadingLabel="Placing order…"
                className="w-full"
              >
                Place Order
              </Button>
              <p className="mt-2 hidden text-center text-xs text-muted md:block">
                You will not be charged — this is a demo store.
              </p>
            </div>
          </div>

          <div className="checkout-actions-spacer" />
        </aside>
      </div>
    </div>
  )
}
