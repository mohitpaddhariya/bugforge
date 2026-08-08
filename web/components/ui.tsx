'use client'

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="spinner" aria-hidden="true" />
      {label ? <span>{label}</span> : null}
      <span className="sr-only">Loading</span>
    </span>
  )
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary'
  loading?: boolean
  loadingLabel?: string
}

export function Button({
  variant = 'primary',
  loading = false,
  loadingLabel = 'Working…',
  disabled,
  className = '',
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || loading}
      className={`btn ${variant === 'primary' ? 'btn-primary' : 'btn-secondary'} ${className}`}
    >
      {loading ? <Spinner label={loadingLabel} /> : children}
    </button>
  )
}

export function Field({
  label,
  hint,
  error,
  className = '',
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  label: string
  hint?: string
  error?: string | null
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1 block text-sm font-medium text-ink">{label}</span>
      <input className="field" {...rest} />
      {hint && !error ? <span className="mt-1 block text-xs text-muted">{hint}</span> : null}
      {error ? <span className="mt-1 block text-xs text-red-600">{error}</span> : null}
    </label>
  )
}

export function Alert({
  tone = 'error',
  children,
  testId,
}: {
  tone?: 'error' | 'info'
  children: ReactNode
  testId?: string
}) {
  const styles =
    tone === 'error'
      ? 'border-red-200 bg-red-50 text-red-700'
      : 'border-blue-200 bg-blue-50 text-blue-800'
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      data-testid={testId}
      className={`rounded-md border px-3 py-2 text-sm ${styles}`}
    >
      {children}
    </div>
  )
}

export function PageHeading({
  title,
  subtitle,
}: {
  title: string
  subtitle?: string
}) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
      {subtitle ? <p className="mt-1 text-sm text-muted">{subtitle}</p> : null}
    </div>
  )
}

export function LoadingBlock({ label = 'Loading…' }: { label?: string }) {
  return (
    <div
      data-testid="loading-block"
      className="flex items-center gap-2 py-16 text-sm text-muted"
    >
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

export function EmptyState({
  title,
  children,
  testId,
}: {
  title: string
  children?: ReactNode
  testId?: string
}) {
  return (
    <div data-testid={testId} className="card px-6 py-12 text-center">
      <p className="text-base font-medium text-ink">{title}</p>
      {children ? <div className="mt-2 text-sm text-muted">{children}</div> : null}
    </div>
  )
}

export function Divider() {
  return <hr className="my-4 border-line" />
}
