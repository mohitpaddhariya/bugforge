'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { login, normalizeError } from '@/components/api-bridge'
import { Alert, Button, Field, PageHeading } from '@/components/ui'

function messageFor(status: number, code: string): string {
  if (status === 401 || status === 400) return 'Email or password is incorrect.'
  if (status === 429) return 'Too many attempts. Please wait a moment and try again.'
  return `We couldn't sign you in (${code}). Please try again.`
}

export default function LoginPage() {
  const router = useRouter()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [next, setNext] = useState('/')

  useEffect(() => {
    try {
      const target = new URLSearchParams(window.location.search).get('next')
      if (target && target.startsWith('/')) setNext(target)
    } catch {
      setNext('/')
    }
  }, [])

  async function onSubmit(e?: React.FormEvent) {
    if (e) e.preventDefault()
    setError(null)
    if (!email.trim() || !password) {
      setError('Enter your email and password.')
      return
    }
    setSubmitting(true)
    try {
      await login(email.trim(), password)
      router.push(next)
      router.refresh()
    } catch (err) {
      const e2 = normalizeError(err)
      setError(messageFor(e2.status, e2.code))
      setSubmitting(false)
    }
  }

  return (
    <div data-testid="login-page" className="mx-auto max-w-sm">
      <PageHeading title="Sign in" subtitle="Use your ShopForge account." />

      <form onSubmit={onSubmit} className="card space-y-4 p-5" data-testid="login-form">
        {error ? <Alert testId="login-error">{error}</Alert> : null}

        <Field
          label="Email"
          type="email"
          name="email"
          autoComplete="username"
          data-testid="login-email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <Field
          label="Password"
          type="password"
          name="password"
          autoComplete="current-password"
          data-testid="login-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <Button
          type="submit"
          data-testid="login-submit"
          loading={submitting}
          loadingLabel="Signing in…"
          className="w-full"
        >
          Sign in
        </Button>
      </form>
    </div>
  )
}
