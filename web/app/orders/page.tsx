'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { getOrders, normalizeError, type Order } from '@/components/api-bridge'
import { formatDate, money } from '@/components/format'
import { Alert, EmptyState, LoadingBlock, PageHeading } from '@/components/ui'

export default function OrderHistoryPage() {
  const router = useRouter()
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    getOrders()
      .then((rows) => {
        if (alive) setOrders(rows)
      })
      .catch((err) => {
        const e = normalizeError(err)
        if (!alive) return
        if (e.status === 401) {
          router.push('/login?next=/orders')
          return
        }
        setError(`We couldn't load your orders (${e.code}).`)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [router])

  if (loading) return <LoadingBlock label="Loading your orders…" />

  return (
    <div data-testid="orders-page">
      <PageHeading title="Order history" />

      {error ? (
        <div className="mb-4">
          <Alert testId="orders-error">{error}</Alert>
        </div>
      ) : null}

      {orders.length === 0 ? (
        <EmptyState testId="orders-empty" title="You have not placed any orders yet.">
          <Link href="/" data-testid="orders-continue-shopping" className="text-accent underline">
            Browse products
          </Link>
        </EmptyState>
      ) : (
        <ul data-testid="orders-list" className="card divide-y divide-line">
          {orders.map((order) => (
            <li key={order.id} data-testid={`order-row-${order.id}`}>
              <Link
                href={`/orders/${order.id}`}
                data-testid={`order-link-${order.id}`}
                className="flex items-center justify-between gap-4 px-4 py-4 hover:bg-gray-50"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink">Order #{order.id}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    {formatDate(order.created_at)}
                    {order.status ? ` · ${order.status}` : ''}
                    {order.coupon_code ? ` · ${order.coupon_code}` : ''}
                  </p>
                </div>
                <div
                  data-testid={`order-total-${order.id}`}
                  className="shrink-0 text-sm font-semibold text-ink"
                >
                  {money(order.total_cents)}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
