'use client'

import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { addToCart, getProduct, normalizeError, type Product } from '@/components/api-bridge'
import { ProductImage } from '@/components/product-card'
import { QuantityInput } from '@/components/quantity-input'
import { money } from '@/components/format'
import { Alert, Button, LoadingBlock } from '@/components/ui'

export default function ProductDetailPage() {
  const params = useParams<{ id: string }>()
  const router = useRouter()
  const id = String(params?.id ?? '')

  const [product, setProduct] = useState<Product | null>(null)
  const [qty, setQty] = useState(1)
  const [loading, setLoading] = useState(true)
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let alive = true
    setLoading(true)
    getProduct(id)
      .then((p) => {
        if (alive) setProduct(p)
      })
      .catch((err) => {
        const e = normalizeError(err)
        if (alive) {
          setError(
            e.status === 404
              ? "We couldn't find that product."
              : `We couldn't load this product (${e.code}).`
          )
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [id])

  async function onAddToCart() {
    if (!product) return
    setError(null)
    setAdded(false)
    setAdding(true)
    try {
      await addToCart(product.id, qty)
      setAdded(true)
    } catch (err) {
      const e = normalizeError(err)
      if (e.status === 401) {
        router.push(`/login?next=/product/${product.id}`)
        return
      }
      setError(`We couldn't add that to your cart (${e.code}).`)
    } finally {
      setAdding(false)
    }
  }

  if (loading) return <LoadingBlock label="Loading product…" />

  if (!product) {
    return (
      <div data-testid="product-detail-page">
        <Alert testId="product-error">{error || "We couldn't find that product."}</Alert>
        <p className="mt-4">
          <Link href="/" data-testid="back-to-products" className="text-sm text-accent underline">
            Back to all products
          </Link>
        </p>
      </div>
    )
  }

  const outOfStock = typeof product.stock === 'number' && product.stock <= 0

  return (
    <div data-testid="product-detail-page">
      <nav className="mb-6 text-sm text-muted">
        <Link href="/" data-testid="back-to-products" className="hover:text-ink">
          Products
        </Link>
        <span className="px-2">/</span>
        <span className="text-ink">{product.name}</span>
      </nav>

      <div className="grid gap-8 md:grid-cols-2">
        <div className="aspect-[4/3] w-full overflow-hidden rounded-lg border border-line bg-gray-100">
          <ProductImage product={product} />
        </div>

        <div>
          {product.category ? (
            <p className="text-xs uppercase tracking-wide text-muted">{product.category}</p>
          ) : null}
          <h1 data-testid="product-name" className="mt-1 text-2xl font-semibold tracking-tight">
            {product.name}
          </h1>
          <p data-testid="product-price" className="mt-2 text-xl font-semibold">
            {money(product.price_cents)}
          </p>
          {product.sku ? <p className="mt-1 text-xs text-muted">SKU {product.sku}</p> : null}

          {product.description ? (
            <p data-testid="product-description" className="mt-4 text-sm leading-6 text-gray-700">
              {product.description}
            </p>
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <QuantityInput
              id="product"
              value={qty}
              max={typeof product.stock === 'number' && product.stock > 0 ? product.stock : 99}
              disabled={outOfStock}
              onChange={setQty}
            />
            <Button
              data-testid="add-to-cart"
              onClick={onAddToCart}
              loading={adding}
              loadingLabel="Adding…"
              disabled={outOfStock}
            >
              {outOfStock ? 'Out of stock' : 'Add to cart'}
            </Button>
          </div>

          {typeof product.stock === 'number' ? (
            <p className="mt-2 text-xs text-muted">
              {outOfStock ? 'Currently unavailable.' : `${product.stock} in stock`}
            </p>
          ) : null}

          {error ? (
            <div className="mt-4">
              <Alert testId="add-to-cart-error">{error}</Alert>
            </div>
          ) : null}

          {added ? (
            <div className="mt-4" data-testid="add-to-cart-success">
              <Alert tone="info">
                Added to your cart.{' '}
                <Link href="/cart" data-testid="go-to-cart" className="underline">
                  View cart
                </Link>
              </Alert>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
