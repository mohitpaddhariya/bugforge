import './globals.css'
import type { Metadata } from 'next'
import { TelemetryProvider } from './telemetry-provider'
import { PromoBanner } from '@/components/promo-banner'
import { SiteHeader } from '@/components/site-header'

export const metadata: Metadata = {
  title: 'ShopForge',
  description: 'ShopForge — a small store.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-full bg-white text-ink antialiased">
        <TelemetryProvider>
          <PromoBanner />
          <SiteHeader />
          <main className="mx-auto w-full max-w-page px-4 py-8">{children}</main>
          <footer className="mt-12 border-t border-line">
            <div className="mx-auto max-w-page px-4 py-6 text-xs text-muted">
              ShopForge — demo store. Orders are not real.
            </div>
          </footer>
        </TelemetryProvider>
      </body>
    </html>
  )
}
