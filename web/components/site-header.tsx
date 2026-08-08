'use client'

import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { getMe, logout, type User } from '@/components/api-bridge'

export function SiteHeader() {
  const pathname = usePathname()
  const router = useRouter()
  const [user, setUser] = useState<User | null>(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    let alive = true
    getMe()
      .then((u) => {
        if (alive) setUser(u && (u as any).id ? u : null)
      })
      .catch(() => {
        if (alive) setUser(null)
      })
      .finally(() => {
        if (alive) setChecked(true)
      })
    return () => {
      alive = false
    }
  }, [pathname])

  async function onLogout() {
    try {
      await logout()
    } catch {
      // Logging out is best-effort; the cookie is cleared server-side.
    }
    setUser(null)
    router.push('/login')
  }

  const link = (href: string, testId: string, label: string) => {
    const active = href === '/' ? pathname === '/' : pathname.startsWith(href)
    return (
      <Link
        href={href}
        data-testid={testId}
        className={`rounded px-2 py-1 text-sm ${
          active ? 'font-semibold text-ink' : 'text-muted hover:text-ink'
        }`}
      >
        {label}
      </Link>
    )
  }

  return (
    <header className="border-b border-line bg-white">
      <div className="mx-auto flex max-w-page items-center justify-between gap-4 px-4 py-3">
        <Link href="/" data-testid="nav-logo" className="text-base font-semibold tracking-tight">
          ShopForge
        </Link>

        <nav className="flex items-center gap-1">
          {link('/', 'nav-products', 'Products')}
          {link('/cart', 'nav-cart', 'Cart')}
          {link('/orders', 'nav-orders', 'Orders')}
        </nav>

        <div className="flex items-center gap-2 text-sm">
          {!checked ? (
            <span className="text-muted">…</span>
          ) : user ? (
            <>
              <span data-testid="header-user" className="hidden text-muted sm:inline">
                {user.name || user.email}
              </span>
              <button
                type="button"
                data-testid="logout-button"
                onClick={onLogout}
                className="rounded border border-line px-2 py-1 text-sm text-ink hover:bg-gray-50"
              >
                Log out
              </button>
            </>
          ) : (
            <Link
              href="/login"
              data-testid="nav-login"
              className="rounded border border-line px-2 py-1 text-sm text-ink hover:bg-gray-50"
            >
              Log in
            </Link>
          )}
        </div>
      </div>
    </header>
  )
}
