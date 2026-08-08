'use client'

import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { getOrder, normalizeError, type Order, type OrderItem } from '@/components/api-bridge'
import { OrderSummary } from '@/components/order-summary'
import { formatDateTime, money } from '@/components/format'
import { Alert, LoadingBlock, PageHeading } from '@/components/ui'

function itemName(item: OrderItem): string {
  return item.name_snapshot || item.name || `Item #${item.product_id}`
}

export default function OrderDetailPage() {
  const params = useParams<{ id: string }>()
  const router = useRouter()
  const id = String(params?.id ?? '')

  const [order, setOrder] = useState<Order | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let alive = true
    setLoading(true)
    getOrder(id)
      .then((o) => {
        if (alive) setOrder(o)
      })
      .catch((err) => {
        const e = normalizeError(err)
        if (!alive) return
        if (e.status === 401) {
          router.push(`/login?next=/orders/${id}`)
          return
        }
        setError(
          e.status === 404
            ? "We couldn't find that order."
            : `We couldn't load this order (${e.code}).`
        )
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [id, router])

  if (loading) return <LoadingBlock label="Loading order…" />

  if (!order) {
    return (
      <div data-testid="order-detail-page">
        <Alert testId="order-error">{error || "We couldn't find that order."}</Alert>
        <p className="mt-4">
          <Link href="/orders" data-testid="back-to-orders" className="text-sm text-accent underline">
            Back to order history
          </Link>
        </p>
      </div>
    )
  }

  const items = order.items ?? []

  return (
    <div data-testid="order-detail-page">
      <nav className="mb-6 text-sm text-muted">
        <Link href="/orders" data-testid="back-to-orders" className="hover:text-ink">
          Order history
        </Link>
        <span className="px-2">/</span>
        <span className="text-ink">Order #{order.id}</span>
      </nav>

      <PageHeading
        title={`Order #${order.id}`}
        subtitle="Thank you — your order is confirmed."
      />

      <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
        <section className="card p-5">
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-muted">Placed</dt>
              <dd data-testid="order-placed-at" className="mt-0.5 font-medium text-ink">
                {formatDateTime(order.created_at)}
              </dd>
            </div>
            <div>
              <dt className="text-muted">Status</dt>
              <dd data-testid="order-status" className="mt-0.5 font-medium capitalize text-ink">
                {order.status || 'placed'}
              </dd>
            </div>
            {order.coupon_code ? (
              <div>
                <dt className="text-muted">Coupon</dt>
                <dd data-testid="order-coupon" className="mt-0.5 font-medium text-ink">
                  {order.coupon_code}
                </dd>
              </div>
            ) : null}
          </dl>

          <h2 className="mt-8 text-sm font-semibold text-ink">Items</h2>
          <ul data-testid="order-items" className="mt-3 divide-y divide-line border-y border-line">
            {items.map((item, index) => (
              <li
                key={item.id ?? `${item.product_id}-${index}`}
                data-testid={`order-item-${item.id ?? index}`}
                className="flex items-center justify-between gap-4 py-3 text-sm"
              >
                <span className="min-w-0 truncate">
                  {itemName(item)} <span className="text-muted">× {item.qty}</span>
                </span>
                <span className="shrink-0 font-medium">
                  {money(Number(item.unit_price_cents) * Number(item.qty))}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <aside>
          <div className="card p-4">
            <h2 className="mb-3 text-sm font-semibold text-ink">Order summary</h2>
            <OrderSummary
              prefix="order-summary"
              subtotalCents={order.subtotal_cents}
              discountCents={order.discount_cents}
              taxCents={order.tax_cents}
              totalCents={order.total_cents}
              couponCode={order.coupon_code}
            />
          </div>

          <p className="mt-4 text-center text-xs text-muted">
            Questions about this order? Contact support with order #{order.id}.
          </p>
        </aside>
      </div>
    </div>
  )
}
