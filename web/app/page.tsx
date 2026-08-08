'use client'

import { useEffect, useMemo, useState } from 'react'
import { getProducts, normalizeError, type Product } from '@/components/api-bridge'
import { ProductCard } from '@/components/product-card'
import { Alert, EmptyState, LoadingBlock, PageHeading } from '@/components/ui'

export default function ProductGridPage() {
  const [products, setProducts] = useState<Product[]>([])
  const [category, setCategory] = useState<string>('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    getProducts()
      .then((rows) => {
        if (alive) setProducts(rows)
      })
      .catch((err) => {
        const e = normalizeError(err)
        if (alive) setError(`We couldn't load the catalog (${e.code}).`)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const categories = useMemo(() => {
    const set = new Set<string>()
    for (const p of products) if (p.category) set.add(p.category)
    return ['all', ...Array.from(set).sort()]
  }, [products])

  const visible = useMemo(
    () => (category === 'all' ? products : products.filter((p) => p.category === category)),
    [products, category]
  )

  return (
    <div data-testid="product-grid-page">
      <PageHeading title="All products" subtitle="Everything we currently stock." />

      {error ? <Alert testId="products-error">{error}</Alert> : null}

      {categories.length > 2 ? (
        <div className="mb-6 flex flex-wrap gap-2">
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              data-testid={`category-filter-${c}`}
              onClick={() => setCategory(c)}
              className={`rounded-full border px-3 py-1 text-sm capitalize ${
                category === c
                  ? 'border-ink bg-ink text-white'
                  : 'border-line bg-white text-ink hover:bg-gray-50'
              }`}
            >
              {c}
            </button>
          ))}
        </div>
      ) : null}

      {loading ? (
        <LoadingBlock label="Loading products…" />
      ) : visible.length === 0 ? (
        <EmptyState testId="products-empty" title="Nothing to show here yet." />
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {visible.map((p) => (
            <ProductCard key={p.id} product={p} />
          ))}
        </div>
      )}
    </div>
  )
}
