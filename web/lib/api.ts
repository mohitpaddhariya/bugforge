/**
 * Thin typed client for the ShopForge API.
 *
 * Every call goes through the global `fetch` — which the telemetry tracker has
 * wrapped — so each request is timed, sized, and stamped with the current
 * interaction's X-Trace-Id. Nothing here bypasses that on purpose.
 *
 * Auth is an httpOnly cookie ("sf_session"), so every request uses
 * credentials: "include".
 *
 * Money is integer cents everywhere. Fields end in _cents.
 */

const API_BASE = (
  (typeof process !== "undefined" &&
    process.env &&
    process.env.NEXT_PUBLIC_API_URL) ||
  "http://localhost:8000"
).replace(/\/+$/, "");

/* ------------------------------------------------------------------ types */

export interface User {
  id: number;
  email: string;
  name: string;
  locale: string;
  created_at?: string;
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  description: string;
  price_cents: number;
  category: string;
  image_url: string;
  stock: number;
}

export interface CartItem {
  id: number;
  product_id: number;
  name: string;
  qty: number;
  unit_price_cents: number;
  line_total_cents: number;
  image_url?: string;
  sku?: string;
}

export interface AppliedCoupon {
  code: string;
  kind: "percent" | "fixed";
  value: number;
}

export interface Cart {
  id: number;
  status: "open" | "converted";
  items: CartItem[];
  coupon_code: string | null;
  coupon?: AppliedCoupon | null;
  subtotal_cents: number;
  discount_cents: number;
  tax_cents: number;
  total_cents: number;
}

export interface OrderItem {
  id: number;
  product_id: number;
  name_snapshot: string;
  qty: number;
  unit_price_cents: number;
}

export interface Order {
  id: number;
  user_id: number;
  status: string;
  coupon_code: string | null;
  subtotal_cents: number;
  discount_cents: number;
  tax_cents: number;
  /**
   * Canonical field. BUG-003 (BUG_TOTAL_FIELD_RENAME) makes the API send `total`
   * instead — the type keeps both so the drift is visible in the response shape
   * rather than hidden behind a client-side fallback.
   */
  total_cents: number;
  total?: number;
  created_at: string;
  items?: OrderItem[];
}

export interface LoginResponse {
  user: User;
}

export interface MeResponse {
  user: User;
}

/* ------------------------------------------------------------------ error */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly body: unknown;
  readonly url: string;

  constructor(
    message: string,
    status: number,
    code: string | null,
    body: unknown,
    url: string
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
    this.url = url;
  }
}

/* ----------------------------------------------------------------- engine */

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

async function request<T>(
  method: Method,
  path: string,
  body?: unknown
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const init: RequestInit = {
    method,
    credentials: "include",
    headers: { Accept: "application/json" },
  };

  if (body !== undefined) {
    init.headers = {
      ...(init.headers as Record<string, string>),
      "Content-Type": "application/json",
    };
    init.body = JSON.stringify(body);
  }

  const res = await fetch(url, init);

  const contentType = res.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");

  let payload: unknown = null;
  if (res.status !== 204) {
    if (isJson) {
      try {
        payload = await res.json();
      } catch {
        payload = null;
      }
    } else {
      try {
        payload = await res.text();
      } catch {
        payload = null;
      }
    }
  }

  if (!res.ok) {
    const p = payload as Record<string, unknown> | string | null;
    let code: string | null = null;
    let message = `${method} ${path} failed with ${res.status}`;
    if (p && typeof p === "object") {
      const detail = (p as Record<string, unknown>).detail;
      if (typeof detail === "string") {
        message = detail;
        code = detail;
      } else if (detail && typeof detail === "object") {
        const d = detail as Record<string, unknown>;
        if (typeof d.code === "string") code = d.code;
        if (typeof d.message === "string") message = d.message;
        else if (code) message = code;
      }
      if (!code && typeof (p as Record<string, unknown>).code === "string") {
        code = (p as Record<string, unknown>).code as string;
      }
      if (
        typeof (p as Record<string, unknown>).message === "string" &&
        message.startsWith(method)
      ) {
        message = (p as Record<string, unknown>).message as string;
      }
    } else if (typeof p === "string" && p.trim()) {
      message = p.slice(0, 300);
    }
    throw new ApiError(message, res.status, code, payload, url);
  }

  return payload as T;
}

/* ------------------------------------------------------------------- auth */

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("POST", "/api/auth/login", { email, password });
}

export function logout(): Promise<void> {
  return request<void>("POST", "/api/auth/logout");
}

export function me(): Promise<MeResponse> {
  return request<MeResponse>("GET", "/api/me");
}

/* ---------------------------------------------------------------- catalog */

export function getProducts(): Promise<Product[]> {
  return request<Product[]>("GET", "/api/products");
}

export function getProduct(id: number | string): Promise<Product> {
  return request<Product>("GET", `/api/products/${id}`);
}

/* ------------------------------------------------------------------- cart */

export function getCart(): Promise<Cart> {
  return request<Cart>("GET", "/api/cart");
}

export function addToCart(productId: number, qty = 1): Promise<Cart> {
  return request<Cart>("POST", "/api/cart/items", {
    product_id: productId,
    qty,
  });
}

export function updateCartItem(itemId: number, qty: number): Promise<Cart> {
  return request<Cart>("PATCH", `/api/cart/items/${itemId}`, { qty });
}

export function removeCartItem(itemId: number): Promise<Cart> {
  return request<Cart>("DELETE", `/api/cart/items/${itemId}`);
}

export function applyCoupon(code: string): Promise<Cart> {
  return request<Cart>("POST", "/api/cart/coupon", { code });
}

export function removeCoupon(): Promise<Cart> {
  return request<Cart>("DELETE", "/api/cart/coupon");
}

/* --------------------------------------------------------------- checkout */

export function checkout(): Promise<Order> {
  return request<Order>("POST", "/api/checkout");
}

/* ----------------------------------------------------------------- orders */

export function getOrders(): Promise<Order[]> {
  return request<Order[]>("GET", "/api/orders");
}

export function getOrder(id: number | string): Promise<Order> {
  return request<Order>("GET", `/api/orders/${id}`);
}

/* ----------------------------------------------------------------- extras */

/** Formats integer cents as a display string. Never accepts floats upstream. */
export function formatCents(cents: number | null | undefined): string {
  return `$${(Number(cents) / 100).toFixed(2)}`;
}

export const api = {
  login,
  logout,
  me,
  getProducts,
  getProduct,
  getCart,
  addToCart,
  updateCartItem,
  removeCartItem,
  applyCoupon,
  removeCoupon,
  checkout,
  getOrders,
  getOrder,
};

export default api;
