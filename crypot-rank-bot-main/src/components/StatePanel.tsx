import { AlertTriangle, LoaderCircle } from 'lucide-react'
import { SurfaceCard } from '@/components/SurfaceCard'

interface StatePanelProps {
  title: string
  message: string
  loading?: boolean
}

export function StatePanel({ title, message, loading = false }: StatePanelProps) {
  return (
    <SurfaceCard title={title} eyebrow={loading ? '加载中' : '状态提示'}>
      <div className="flex items-center gap-3 rounded-[22px] border border-white/10 bg-black/20 px-4 py-4 text-zinc-300">
        {loading ? (
          <LoaderCircle className="h-5 w-5 animate-spin text-cyan-300" />
        ) : (
          <AlertTriangle className="h-5 w-5 text-amber-300" />
        )}
        <p className="text-sm leading-7">{message}</p>
      </div>
    </SurfaceCard>
  )
}
