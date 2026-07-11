interface MetricCardProps {
  label: string
  value: string
  change: string
}

export function MetricCard({ label, value, change }: MetricCardProps) {
  const positive = change.startsWith('+')

  return (
    <div className="rounded-[24px] border border-white/10 bg-black/20 p-5">
      <div className="text-xs uppercase tracking-[0.28em] text-zinc-500">{label}</div>
      <div className="mt-4 text-3xl font-semibold text-white">{value}</div>
      <div className={`mt-2 text-sm ${positive ? 'text-emerald-300' : 'text-rose-300'}`}>{change}</div>
    </div>
  )
}
