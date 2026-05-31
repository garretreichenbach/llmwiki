'use client'

import * as React from 'react'
import { useRouter, usePathname } from 'next/navigation'
import { useUserStore, useKBStore } from '@/stores'
import { apiFetch, isApiError } from '@/lib/api'
import { withAuthTimeout } from '@/lib/auth-errors'

const isLocal = process.env.NEXT_PUBLIC_MODE === 'local'

function isAuthFailure(err: unknown): boolean {
  return isApiError(err) && (err.status === 401 || err.status === 403)
}

interface AuthProviderProps {
  userId: string
  email: string
  children: React.ReactNode
}

export function AuthProvider({ userId, email, children }: AuthProviderProps) {
  const router = useRouter()
  const pathname = usePathname()
  const setUser = useUserStore((s) => s.setUser)
  const setAccessToken = useUserStore((s) => s.setAccessToken)
  const setOnboarded = useUserStore((s) => s.setOnboarded)
  const signOut = useUserStore((s) => s.signOut)
  const onboarded = useUserStore((s) => s.onboarded)
  const fetchKBs = useKBStore((s) => s.fetchKBs)

  React.useEffect(() => {
    if (isLocal) {
      setUser({ id: userId, email })
      setAccessToken('local')
      setOnboarded(true)
      fetchKBs()
      return
    }

    let cancelled = false
    let subscription: { unsubscribe: () => void } | undefined

    import('@/lib/supabase/client').then(({ createClient }) => {
      if (cancelled) return
      const supabase = createClient()

      const bounceToLogin = async () => {
        try { await supabase.auth.signOut() } catch { /* best-effort */ }
        signOut()
        useKBStore.setState({ knowledgeBases: [], loading: false, error: null })
        if (!cancelled) router.replace('/login')
      }

      ;(async () => {
        try {
          const { data: { user: authUser } } = await withAuthTimeout(supabase.auth.getUser())
          if (cancelled) return
          if (!authUser) {
            await bounceToLogin()
            return
          }
          const { data: { session } } = await withAuthTimeout(supabase.auth.getSession())
          if (cancelled) return
          if (!session) {
            await bounceToLogin()
            return
          }
          setUser({ id: userId, email })
          setAccessToken(session.access_token)
          try {
            await fetchKBs({ throwOnError: true })
          } catch (err) {
            if (isAuthFailure(err)) {
              await bounceToLogin()
              return
            }
          }

          try {
            const me = await apiFetch<{ onboarded: boolean }>('/v1/me', session.access_token)
            if (cancelled) return
            setOnboarded(me.onboarded)
            if (!me.onboarded && pathname !== '/onboarding') {
              router.replace('/onboarding')
            }
          } catch {
            const stored = useUserStore.getState().onboarded
            if (stored === null) setOnboarded(true)
          }
        } catch {
          await bounceToLogin()
        }
      })()

      const { data } = supabase.auth.onAuthStateChange((_event, session) => {
        if (cancelled) return
        if (session) {
          useUserStore.getState().setAccessToken(session.access_token)
        } else {
          signOut()
          useKBStore.setState({ knowledgeBases: [], loading: false, error: null })
          router.replace('/login')
        }
      })
      subscription = data.subscription
    })

    return () => {
      cancelled = true
      subscription?.unsubscribe()
    }
  }, [userId, email, setUser, setAccessToken, setOnboarded, fetchKBs, router, pathname, signOut])

  React.useEffect(() => {
    if (!isLocal && onboarded === false && pathname !== '/onboarding') {
      router.replace('/onboarding')
    }
  }, [onboarded, pathname, router])

  return <>{children}</>
}
