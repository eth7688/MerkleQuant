import { Link, NavLink } from 'react-router-dom'
import { Activity, Compass, RefreshCcw, Sparkles } from 'lucide-react'
import { useDemoStore } from '@/hooks/useDemoStore'
import { cn } from '@/lib/utils'

interface AppShellProps {
  title: string
  subtitle: string
  onRefresh?: () => void
  refreshing?: boolean
  children: React.ReactNode
}

const navItems = [
  { to: '/', label: '首页工作台' },
  { to: '/funding', label: '融资雷达' },
  { to: '/opportunities', label: '机会页' },
]

export function AppShell({
  title,
  subtitle,
  onRefresh,
  refreshing = false,
  children,
}: AppShellProps) {
  const lastRefreshAt = useDemoStore((state) => state.lastRefreshAt)
  const sources = useDemoStore((state) => state.sources)

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,#15374d_0%,#071019_38%,#02040a_100%)] text-zinc-100">
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(rgba(48,96,112,0.18)_1px,transparent_1px),linear-gradient(90deg,rgba(48,96,112,0.14)_1px,transparent_1px)] bg-[size:46px_46px] opacity-25" />
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(12,225,198,0.15),transparent_30%),radial-gradient(circle_at_80%_10%,rgba(123,195,255,0.16),transparent_24%),radial-gradient(circle_at_50%_80%,rgba(17,124,148,0.18),transparent_30%)]" />

      <div className="relative mx-auto flex min-h-screen max-w-7xl flex-col px-6 pb-10 pt-8 lg:px-10">
        <header className="mb-8 grid gap-6 rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-[0_0_80px_rgba(0,0,0,0.35)] backdrop-blur-xl lg:grid-cols-[1.3fr_0.7fr]">
          <div className="space-y-5">
            <div className="flex items-center gap-3 text-sm uppercase tracking-[0.4em] text-cyan-200/70">
              <Sparkles className="h-4 w-4" />
              中文 CryptoRank Demo
            </div>
            <div>
              <h1 className="max-w-3xl font-serif text-4xl font-semibold leading-tight text-white md:text-5xl">
                {title}
              </h1>
              <p className="mt-3 max-w-2xl text-sm leading-7 text-zinc-300 md:text-base">
                {subtitle}
              </p>
            </div>
            <nav className="flex flex-wrap gap-3">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'rounded-full border px-4 py-2 text-sm transition',
                      isActive
                        ? 'border-cyan-300/80 bg-cyan-300/10 text-cyan-100 shadow-[0_0_25px_rgba(73,216,230,0.15)]'
                        : 'border-white/10 bg-white/5 text-zinc-300 hover:border-cyan-200/40 hover:text-white',
                    )
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>

          <div className="flex flex-col justify-between gap-6 rounded-[28px] border border-cyan-200/10 bg-black/20 p-5">
            <div className="grid gap-3 text-sm text-zinc-300">
              <div className="flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <Activity className="h-4 w-4 text-cyan-300" />
                <div>
                  <div className="text-xs uppercase tracking-[0.3em] text-zinc-500">状态</div>
                  <div className="mt-1 text-zinc-100">免费层数据代理在线</div>
                </div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <div className="text-xs uppercase tracking-[0.3em] text-zinc-500">最近刷新</div>
                <div className="mt-1 text-zinc-100">
                  {lastRefreshAt ? new Date(lastRefreshAt).toLocaleString('zh-CN') : '等待首次加载'}
                </div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <div className="text-xs uppercase tracking-[0.3em] text-zinc-500">数据来源</div>
                <div className="mt-1 line-clamp-3 text-xs leading-6 text-zinc-300">
                  {sources.length ? sources.join(' · ') : '页面首屏数据 + 前台公开接口'}
                </div>
              </div>
            </div>

            <div className="flex items-center justify-between gap-3">
              <Link
                to="/opportunities"
                className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm text-zinc-100 transition hover:border-cyan-200/40 hover:bg-white/10"
              >
                <Compass className="h-4 w-4 text-cyan-300" />
                直接看今日机会
              </Link>
              <button
                type="button"
                onClick={onRefresh}
                className="inline-flex items-center gap-2 rounded-full border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm text-cyan-50 transition hover:bg-cyan-300/20 disabled:cursor-not-allowed disabled:opacity-70"
                disabled={!onRefresh || refreshing}
              >
                <RefreshCcw className={cn('h-4 w-4', refreshing && 'animate-spin')} />
                {refreshing ? '刷新中' : '刷新数据'}
              </button>
            </div>
          </div>
        </header>

        <main className="flex-1">{children}</main>
      </div>
    </div>
  )
}
