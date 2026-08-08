'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useMemo, useState } from 'react'
import {
  getCart,
  normalizeError,
  removeCartItem,
  updateCartItem,
  type Cart,
  type CartItem,
} from '@/components/api-bridge'
import { CouponForm } from '@/components/coupon-form'
import { OrderSummary } from '@/components/order-summary'
import { ProductImage } from '@/components/product-card'
import { QuantityInput } from '@/components/quantity-input'
import { money } from '@/components/format'
import { Alert, EmptyState, LoadingBlock, PageHeading } from '@/components/ui'

function itemName(item: CartItem): string {
  return item.name || item.product?.name || `Item #${item.product_id}`
}

function lineTotal(item: CartItem): number {
  if (typeof item.line_total_cents === 'number') return item.line_total_cents
  return Number(item.unit_price_cents) * Number(item.qty)
}

export default function CartPage() {
  const router = useRouter()
  const [cart, setCart] = useState<Cart | null>(null)
  const [loading, setLoading] = useState(true)
  const [busyItem, setBusyItem] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  function handleAuth(err: any): boolean {
    const e = normalizeError(err)
    if (e.status === 401) {
      router.push('/login?next=/cart')
      return true
    }
    return false
  }

  useEffect(() => {
    let alive = true
    getCart()
      .then((c) => {
        if (alive) setCart(c)
      })
      .catch((err) => {
        if (handleAuth(err)) return
        const e = normalizeError(err)
        if (alive) setError(`We couldn't load your cart (${e.code}).`)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const items = cart?.items ?? []

  const computedSubtotal = useMemo(
    () => items.reduce((sum, item) => sum + lineTotal(item), 0),
    [items]
  )

  async function onQty(item: CartItem, qty: number) {
    setError(null)
    setBusyItem(item.id)
    try {
      const next = await updateCartItem(item.id, qty)
      setCart(next)
    } catch (err) {
      if (handleAuth(err)) return
      const e = normalizeError(err)
      setError(`We couldn't update that quantity (${e.code}).`)
    } finally {
      setBusyItem(null)
    }
  }

  async function onRemove(item: CartItem) {
    setError(null)
    setBusyItem(item.id)
    try {
      const next = await removeCartItem(item.id)
      setCart(next)
    } catch (err) {
      if (handleAuth(err)) return
      const e = normalizeError(err)
      setError(`We couldn't remove that item (${e.code}).`)
    } finally {
      setBusyItem(null)
    }
  }

  if (loading) return <LoadingBlock label="Loading your cart…" />

  return (
    <div data-testid="cart-page">
      <PageHeading title="Your cart" />

      {error ? (
        <div className="mb-4">
          <Alert testId="cart-error">{error}</Alert>
        </div>
      ) : null}

      {items.length === 0 ? (
        <EmptyState testId="cart-empty" title="Your cart is empty.">
          <Link href="/" data-testid="cart-continue-shopping" className="text-accent underline">
            Browse products
          </Link>
        </EmptyState>
      ) : (
        <div className="grid gap-8 lg:grid-cols-[1fr_320px]">
          <ul data-testid="cart-items" className="card divide-y divide-line">
            {items.map((item) => (
              <li
                key={item.id}
                data-testid={`cart-item-${item.id}`}
                className="flex items-start gap-4 p-4"
              >
                <div className="h-16 w-16 shrink-0 overflow-hidden rounded border border-line bg-gray-100">
                  <ProductImage
                    product={{ name: itemName(item), image_url: item.image_url || item.product?.image_url }}
                  />
                </div>

                <div className="min-w-0 flex-1">
                  <Link
                    href={`/product/${item.product_id}`}
                    data-testid={`cart-item-link-${item.id}`}
                    className="block truncate text-sm font-medium text-ink hover:underline"
                  >
                    {itemName(item)}
                  </Link>
                  <p className="mt-0.5 text-xs text-muted">
                    {money(item.unit_price_cents)} each
                  </p>

                  <div className="mt-3 flex items-center gap-3">
                    <QuantityInput
                      id={item.id}
                      value={Number(item.qty)}
                      disabled={busyItem === item.id}
                      onChange={(qty) => onQty(item, qty)}
                    />
                    <button
                      type="button"
                      data-testid={`cart-item-remove-${item.id}`}
                      disabled={busyItem === item.id}
                      onClick={() => onRemove(item)}
                      className="text-sm text-muted underline hover:text-ink disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </div>
                </div>

                <div
                  data-testid={`cart-item-total-${item.id}`}
                  className="shrink-0 text-sm font-semibold text-ink"
                >
                  {money(lineTotal(item))}
                </div>
              </li>
            ))}
          </ul>

          <aside className="space-y-4">
            <CouponForm
              appliedCode={cart?.coupon_code}
              onCart={(next) => setCart(next)}
            />

            <div className="card p-4">
              <OrderSummary
                prefix="cart-summary"
                subtotalCents={cart?.subtotal_cents ?? computedSubtotal}
                discountCents={cart?.discount_cents ?? 0}
                taxCents={cart?.tax_cents ?? 0}
                totalCents={
                  cart?.total_cents ??
                  (cart?.subtotal_cents ?? computedSubtotal) -
                    Number(cart?.discount_cents || 0) +
                    Number(cart?.tax_cents || 0)
                }
                couponCode={cart?.coupon_code}
              />

              <Link
                href="/checkout"
                data-testid="checkout-link"
                className="btn btn-primary mt-4 w-full"
              >
                Proceed to checkout
              </Link>
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}
