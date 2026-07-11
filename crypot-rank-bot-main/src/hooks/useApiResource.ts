import { useCallback, useEffect, useState } from 'react'
import { useDemoStore } from '@/hooks/useDemoStore'
import type { ApiEnvelope } from '@shared/cryptorank'

interface UseApiResourceState<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => Promise<void>
}

export function useApiResource<T>(endpoint: string): UseApiResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const setMeta = useDemoStore((state) => state.setMeta)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const response = await fetch(endpoint)
      const payload = (await response.json()) as ApiEnvelope<T> & { error?: string }

      if (!response.ok) {
        throw new Error(payload.error || '请求失败')
      }

      setData(payload.data)
      setMeta(payload.updatedAt, payload.source)
    } catch (err) {
      const message = err instanceof Error ? err.message : '请求失败'
      setError(message)
    } finally {
      setLoading(false)
    }
  }, [endpoint, setMeta])

  useEffect(() => {
    void load()
  }, [load])

  return { data, loading, error, reload: load }
}
