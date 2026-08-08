'use client'

import { useEffect, useState } from 'react'
import { getFlags } from '@/components/api-bridge'

export type Flags = Record<string, boolean>

/**
 * Feature flags are the bug switches. They are read from
 * `GET /api/debug/flags` on mount and re-read on an interval so a flag
 * flipped at runtime takes effect without a rebuild or a hard reload.
 * The debug routes are excluded from telemetry, so this polling does not
 * pollute the timeline the robot reads.
 */
export function useFlags(): { flags: Flags; loaded: boolean } {
  const [flags, setFlags] = useState<Flags>({})
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let alive = true

    async function read() {
      try {
        const next = await getFlags()
        if (alive) setFlags(next)
      } catch {
        // The control plane being unreachable must never break the store.
      } finally {
        if (alive) setLoaded(true)
      }
    }

    read()
    const timer = setInterval(read, 5000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  return { flags, loaded }
}

export function flagOn(flags: Flags, key: string): boolean {
  return flags[key] === true
}
