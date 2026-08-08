'use client'

/**
 * Thin adapter over `@/lib/api`.
 *
 * The store UI talks to the backend exclusively through this module. It
 * prefers the helper exported by `lib/api` when one exists and otherwise
 * issues the request itself, so a naming difference on either side never
 * takes a page down. Errors are normalised into `ApiError` so pages can
 * branch on the HTTP status and the machine-readable `code` the API returns
 * (e.g. `{"error": "coupon_expired"}`).
 */

import * as apiModule from '@/lib/api'

const api: Record<string, any> = (apiModule as unknown as Record<string, any>) || {}

export const API_BASE =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) ||
  'http://localhost:8000'

export class ApiError extends Error {
  status: number
  code: string
  body: any

  constructor(status: number, code: string, body?: any) {
    super(code || `http_${status}`)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.body = body
  }
}

function pickCode(body: any): string {
  if (!body) return ''
  if (typeof body === 'string') return body
  if (typeof body.error === 'string') return body.error
  if (typeof body.code === 'string') return body.code
  if (typeof body.detail === 'string') return body.detail
  if (body.detail && typeof body.detail.error === 'string') return body.detail.error
  return ''
}

export function normalizeError(err: any): ApiError {
  if (err instanceof ApiError) return err
  const status =
    Number(err?.status ?? err?.statusCode ?? err?.response?.status ?? 0) || 0
  const body = err?.body ?? err?.data ?? err?.payload ?? err?.response?.data ?? null
  const code = pickCode(body) || pickCode(err) || err?.message || 'request_failed'
  const e = new ApiError(status, code, body)
  if (err?.stack) e.stack = err.stack
  return e
}

async function request(path: string, init: RequestInit = {}): Promise<any> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (init.body != null) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: { ...headers, ...((init.headers as Record<string, string>) || {}) },
  })
  const text = await res.text()
  let body: any = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!res.ok) throw new ApiError(res.status, pickCode(body) || `http_${res.status}`, body)
  return body
}

function resolve(names: string[]): ((...args: any[]) => any) | null {
  for (const n of names) {
    const f = api[n]
    if (typeof f === 'function') return f
  }
  return null
}

async function via<T>(names: string[], args: any[], fallback: () => Promise<any>): Promise<T> {
  const f = resolve(names)
  try {
    return (f ? await f(...args) : await fallback()) as T
  } catch (err) {
    throw normalizeError(err)
  }
}

/* ------------------------------------------------------------------ shapes */

export type Product = {
  id: number
  sku?: string
  name: string
  description?: string
  price_cents: number
  category?: string
  image_url?: string
  stock?: number
}

export type CartItem = {
  id: number
  product_id: number
  name?: string
  qty: number
  unit_price_cents: number
  line_total_cents?: number
  image_url?: string
  product?: Partial<Product>
}

export type Cart = {
  id?: number
  items: CartItem[]
  coupon_code?: string | null
  subtotal_cents?: number
  discount_cents?: number
  tax_cents?: number
  total_cents?: number
}

export type OrderItem = {
  id?: number
  product_id?: number
  name_snapshot?: string
  name?: string
  qty: number
  unit_price_cents: number
}

export type Order = {
  id: number
  status?: string
  created_at?: string
  coupon_code?: string | null
  subtotal_cents?: number
  discount_cents?: number
  tax_cents?: number
  /** Read unconditionally by the order pages — see BUG-003. */
  total_cents?: number
  items?: OrderItem[]
}

export type User = {
  id: number
  email: string
  name?: string
  locale?: string
}

/* ------------------------------------------------------------- unwrapping */

function unwrapList(payload: any, key: string): any[] {
  if (Array.isArray(payload)) return payload
  if (payload && Array.isArray(payload[key])) return payload[key]
  if (payload && Array.isArray(payload.items)) return payload.items
  if (payload && Array.isArray(payload.data)) return payload.data
  return []
}

function unwrapOne(payload: any, key: string): any {
  if (!payload) return payload
  if (payload[key] && typeof payload[key] === 'object') return payload[key]
  return payload
}

function buildCart(payload: any): Cart {
  const cart = unwrapOne(payload, 'cart') || {}
  return { ...cart, items: unwrapList(cart, 'items') } as Cart
}

function looksLikeCart(payload: any): boolean {
  if (!payload || typeof payload !== 'object') return false
  const c = payload.cart && typeof payload.cart === 'object' ? payload.cart : payload
  return Array.isArray(c.items) || typeof c.subtotal_cents === 'number'
}

/**
 * Cart mutations are expected to echo the updated cart. When the endpoint
 * answers with something else (a bare `{"ok": true}`, say) re-read the cart
 * so the page never renders a phantom empty basket.
 */
async function toCart(payload: any): Promise<Cart> {
  if (looksLikeCart(payload)) return buildCart(payload)
  const fresh = await via<any>(['getCart', 'fetchCart'], [], () => request('/api/cart'))
  return buildCart(fresh)
}

/* ------------------------------------------------------------------- calls */

export async function login(email: string, password: string): Promise<User> {
  const res = await via<any>(['login', 'authLogin', 'signIn'], [email, password], () =>
    request('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
  )
  return unwrapOne(res, 'user') as User
}

export async function logout(): Promise<void> {
  await via<any>(['logout', 'authLogout', 'signOut'], [], () =>
    request('/api/auth/logout', { method: 'POST' })
  )
}

export async function getMe(): Promise<User> {
  const res = await via<any>(['getMe', 'me', 'fetchMe'], [], () => request('/api/me'))
  return unwrapOne(res, 'user') as User
}

export async function getProducts(): Promise<Product[]> {
  const res = await via<any>(['getProducts', 'fetchProducts', 'listProducts'], [], () =>
    request('/api/products')
  )
  return unwrapList(res, 'products') as Product[]
}

export async function getProduct(id: number | string): Promise<Product> {
  const res = await via<any>(['getProduct', 'fetchProduct'], [id], () =>
    request(`/api/products/${id}`)
  )
  return unwrapOne(res, 'product') as Product
}

export async function getCart(): Promise<Cart> {
  const res = await via<any>(['getCart', 'fetchCart'], [], () => request('/api/cart'))
  return toCart(res)
}

export async function addToCart(productId: number, qty: number): Promise<Cart> {
  const res = await via<any>(
    ['addToCart', 'addCartItem', 'createCartItem'],
    [productId, qty],
    () =>
      request('/api/cart/items', {
        method: 'POST',
        body: JSON.stringify({ product_id: productId, qty }),
      })
  )
  return toCart(res)
}

export async function updateCartItem(itemId: number, qty: number): Promise<Cart> {
  const res = await via<any>(
    ['updateCartItem', 'setCartItemQty', 'patchCartItem'],
    [itemId, qty],
    () =>
      request(`/api/cart/items/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify({ qty }),
      })
  )
  return toCart(res)
}

export async function removeCartItem(itemId: number): Promise<Cart> {
  const res = await via<any>(
    ['removeCartItem', 'deleteCartItem'],
    [itemId],
    () => request(`/api/cart/items/${itemId}`, { method: 'DELETE' })
  )
  return toCart(res)
}

export async function applyCoupon(code: string): Promise<Cart> {
  const res = await via<any>(['applyCoupon', 'addCoupon'], [code], () =>
    request('/api/cart/coupon', { method: 'POST', body: JSON.stringify({ code }) })
  )
  return toCart(res)
}

export async function removeCoupon(): Promise<Cart> {
  const res = await via<any>(['removeCoupon', 'deleteCoupon', 'clearCoupon'], [], () =>
    request('/api/cart/coupon', { method: 'DELETE' })
  )
  return toCart(res)
}

export async function checkout(payload: Record<string, any> = {}): Promise<Order> {
  const res = await via<any>(['checkout', 'placeOrder', 'createOrder'], [payload], () =>
    request('/api/checkout', { method: 'POST', body: JSON.stringify(payload) })
  )
  return unwrapOne(res, 'order') as Order
}

export async function getOrders(): Promise<Order[]> {
  const res = await via<any>(['getOrders', 'fetchOrders', 'listOrders'], [], () =>
    request('/api/orders')
  )
  return unwrapList(res, 'orders') as Order[]
}

export async function getOrder(id: number | string): Promise<Order> {
  const res = await via<any>(['getOrder', 'fetchOrder'], [id], () =>
    request(`/api/orders/${id}`)
  )
  return unwrapOne(res, 'order') as Order
}

export async function getFlags(): Promise<Record<string, boolean>> {
  const res = await via<any>(['getFlags', 'fetchFlags', 'getDebugFlags'], [], () =>
    request('/api/debug/flags')
  )
  return normalizeFlags(res)
}

export function normalizeFlags(payload: any): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  if (!payload) return out
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(payload.flags)
      ? payload.flags
      : null
  if (rows) {
    for (const row of rows) {
      if (row && typeof row.key === 'string') out[row.key] = Boolean(row.enabled)
    }
    return out
  }
  const map =
    payload.flags && typeof payload.flags === 'object' ? payload.flags : payload
  for (const [k, v] of Object.entries(map)) {
    if (typeof v === 'boolean') out[k] = v
    else if (v && typeof v === 'object' && 'enabled' in (v as any)) {
      out[k] = Boolean((v as any).enabled)
    }
  }
  return out
}
