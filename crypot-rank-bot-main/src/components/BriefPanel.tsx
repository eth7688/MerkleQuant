import { Copy, Check } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { BriefItem } from '@shared/cryptorank'
import { SurfaceCard } from '@/components/SurfaceCard'

interface BriefPanelProps {
  title: string
  items: BriefItem[]
}

export function BriefPanel({ title, items }: BriefPanelProps) {
  const [copied, setCopied] = useState(false)
  const [fallbackText, setFallbackText] = useState<string | null>(null)

  const text = useMemo(
    () =>
      items
        .map((item, index) => `${index + 1}. ${item.title}\n${item.description}`)
        .join('\n\n'),
    [items],
  )

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setFallbackText(null)
      window.setTimeout(() => setCopied(false), 1400)
    } catch {
      setFallbackText('当前环境不允许直接写入剪贴板，请手动复制下方摘要。')
    }
  }

  return (
    <SurfaceCard
      title={title}
      eyebrow="今日摘要"
      className="h-full bg-[linear-gradient(180deg,rgba(8,26,36,0.88),rgba(5,11,17,0.7))]"
    >
      <div className="flex items-center justify-between gap-4">
        <p className="max-w-2xl text-sm leading-7 text-zinc-300">
          这块区域适合直接拿去发 X、TG、会员区，或者继续塞进你的 bot / skill 工作流。
        </p>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-2 rounded-full border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm text-cyan-50 transition hover:bg-cyan-300/20"
        >
          {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
          {copied ? '已复制' : '复制摘要'}
        </button>
      </div>

      <div className="mt-5 grid gap-3">
        {fallbackText ? (
          <div className="rounded-[20px] border border-amber-300/20 bg-amber-300/10 px-4 py-3 text-sm text-amber-50">
            {fallbackText}
          </div>
        ) : null}
        {items.map((item) => (
          <div key={item.title} className="rounded-[22px] border border-white/10 bg-white/5 p-4">
            <div className="text-sm font-semibold text-white">{item.title}</div>
            <div className="mt-2 text-sm leading-7 text-zinc-300">{item.description}</div>
          </div>
        ))}
      </div>
    </SurfaceCard>
  )
}
