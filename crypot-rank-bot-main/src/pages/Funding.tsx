import { AppShell } from '@/components/AppShell'
import { BriefPanel } from '@/components/BriefPanel'
import { DataTable } from '@/components/DataTable'
import { LinkBadges } from '@/components/LinkBadges'
import { MetricCard } from '@/components/MetricCard'
import { StatePanel } from '@/components/StatePanel'
import { SurfaceCard } from '@/components/SurfaceCard'
import { useApiResource } from '@/hooks/useApiResource'
import type { FundingData } from '@shared/cryptorank'

export default function Funding() {
  const { data, loading, error, reload } = useApiResource<FundingData>('/api/funding')

  return (
    <AppShell
      title="中文融资雷达"
      subtitle="把近期 Funding Rounds 压成更适合中文创作者和研究者使用的信号面板，帮你判断今天最该继续跟踪谁。"
      onRefresh={reload}
      refreshing={loading}
    >
      {!data && loading ? <StatePanel title="融资雷达" message="正在抓取近期融资数据..." loading /> : null}
      {!data && error ? <StatePanel title="融资雷达" message={error} /> : null}

      {data ? (
        <div className="grid gap-6">
          <section className="grid gap-4 md:grid-cols-3">
            <MetricCard label="BTC 占比" value={`${data.market.btcDominance.toFixed(2)}%`} change={`${data.market.btcDominanceChangePercent >= 0 ? '+' : ''}${data.market.btcDominanceChangePercent.toFixed(2)}%`} />
            <MetricCard label="总市值" value={`$${(data.market.totalMarketCap / 1_000_000_000_000).toFixed(2)}T`} change={`${data.market.totalMarketCapChangePercent >= 0 ? '+' : ''}${data.market.totalMarketCapChangePercent.toFixed(2)}%`} />
            <MetricCard label="24h 成交额" value={`$${(data.market.totalVolume24h / 1_000_000_000).toFixed(2)}B`} change={`${data.market.totalVolume24hChangePercent >= 0 ? '+' : ''}${data.market.totalVolume24hChangePercent.toFixed(2)}%`} />
          </section>

          <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
            <SurfaceCard title="近期融资项目" eyebrow="Funding Rounds">
              <DataTable
                columns={[
                  { key: 'project', label: '项目' },
                  { key: 'stage', label: '阶段' },
                  { key: 'raise', label: '融资额' },
                  { key: 'funds', label: '机构' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.items.map((item) => ({
                  project: item.project,
                  stage: item.stage,
                  raise: item.raise,
                  funds: item.funds,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>
            <BriefPanel title="为什么今天值得看" items={data.highlights} />
          </section>

          <SurfaceCard title="中文信号解释" eyebrow="Operator Notes">
            <div className="grid gap-4 md:grid-cols-3">
              <div className="rounded-[22px] border border-white/10 bg-black/20 p-4">
                <div className="text-sm font-semibold text-white">优先看基金质量</div>
                <p className="mt-2 text-sm leading-7 text-zinc-300">
                  如果一轮融资里出现高层级基金，比单纯金额更值得继续深挖。
                </p>
              </div>
              <div className="rounded-[22px] border border-white/10 bg-black/20 p-4">
                <div className="text-sm font-semibold text-white">阶段比金额更重要</div>
                <p className="mt-2 text-sm leading-7 text-zinc-300">
                  Pre-Seed、Seed 和战略轮更适合做“新机会发现”叙事。
                </p>
              </div>
              <div className="rounded-[22px] border border-white/10 bg-black/20 p-4">
                <div className="text-sm font-semibold text-white">摘要要能直接复用</div>
                <p className="mt-2 text-sm leading-7 text-zinc-300">
                  页面右侧的摘要卡默认就是给你复制到 X、TG 或会员区用的。
                </p>
              </div>
            </div>
          </SurfaceCard>
        </div>
      ) : null}
    </AppShell>
  )
}
