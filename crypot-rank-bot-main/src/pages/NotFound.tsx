import { Link } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { SurfaceCard } from '@/components/SurfaceCard'

export default function NotFound() {
  return (
    <AppShell
      title="这个页面还没有被点亮"
      subtitle="你可以先回到首页工作台，继续看中文雷达、融资和今日机会。"
    >
      <SurfaceCard title="404" eyebrow="Page Missing">
        <div className="space-y-4 text-zinc-300">
          <p className="text-sm leading-7">当前路由不存在，可能是录屏时输入了错误地址。</p>
          <Link
            to="/"
            className="inline-flex rounded-full border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm text-cyan-50 transition hover:bg-cyan-300/20"
          >
            回到首页工作台
          </Link>
        </div>
      </SurfaceCard>
    </AppShell>
  )
}
