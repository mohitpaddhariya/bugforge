'use client'

import Link from 'next/link'
import { money } from '@/components/format'
import type { Product } from '@/components/api-bridge'

export function ProductImage({
  product,
  className = '',
}: {
  product: Partial<Product>
  className?: string
}) {
  const initials = String(product?.name || '?')
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()

  if (product?.image_url) {
    return (
      <img
        src={product.image_url}
        alt={product.name || ''}
        className={`h-full w-full object-cover ${className}`}
      />
    )
  }

  return (
    <div
      className={`flex h-full w-full items-center justify-center bg-gray-100 text-lg font-semibold text-gray-400 ${className}`}
      aria-hidden="true"
    >
      {initials}
    </div>
  )
}

export function ProductCard({ product }: { product: Product }) {
  return (
    <Link
      href={`/product/${product.id}`}
      data-testid={`product-card-${product.id}`}
      className="card group block overflow-hidden transition hover:border-gray-300"
    >
      <div className="aspect-[4/3] w-full overflow-hidden bg-gray-100">
        <ProductImage product={product} />
      </div>
      <div className="p-3">
        {product.category ? (
          <p className="text-xs uppercase tracking-wide text-muted">{product.category}</p>
        ) : null}
        <h2 className="mt-0.5 text-sm font-medium text-ink group-hover:underline">
          {product.name}
        </h2>
        <p
          data-testid={`product-price-${product.id}`}
          className="mt-1 text-sm font-semibold text-ink"
        >
          {money(product.price_cents)}
        </p>
      </div>
    </Link>
  )
}
