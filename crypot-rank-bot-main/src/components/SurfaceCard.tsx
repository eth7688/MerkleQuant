import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface SurfaceCardProps {
  title?: string
  eyebrow?: string
  className?: string
  children: ReactNode
}

export function SurfaceCard({ title, eyebrow, className, children }: SurfaceCardProps) {
  return (
    <section
      className={cn(
        'rounded-[28px] border border-white/10 bg-white/5 p-5 shadow-[0_0_0_1px_rgba(255,255,255,0.02)_inset] backdrop-blur-md',
        className,
      )}
    >
      {(eyebrow || title) && (
        <header className="mb-4">
          {eyebrow ? (
            <div className="mb-2 text-[11px] uppercase tracking-[0.32em] text-cyan-200/70">{eyebrow}</div>
          ) : null}
          {title ? <h2 className="text-lg font-semibold text-white">{title}</h2> : null}
        </header>
      )}
      {children}
    </section>
  )
}
