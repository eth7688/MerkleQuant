import { AppShell } from '@/components/AppShell'
import { BriefPanel } from '@/components/BriefPanel'
import { DataTable } from '@/components/DataTable'
import { LinkBadges } from '@/components/LinkBadges'
import { MetricCard } from '@/components/MetricCard'
import { StatePanel } from '@/components/StatePanel'
import { SurfaceCard } from '@/components/SurfaceCard'
import { useApiResource } from '@/hooks/useApiResource'
import type { RadarData } from '@shared/cryptorank'

export default function Home() {
  const { data, loading, error, reload } = useApiResource<RadarData>('/api/radar')

  return (
    <AppShell
      title="把 CryptoRank 变成中文机会雷达"
      subtitle="不是再造一个数据站，而是做一个适合中文用户的网页工作台：把市场、融资、Upcoming、空投和今日重点压成可直接行动的入口。"
      onRefresh={reload}
      refreshing={loading}
    >
      {!data && loading ? <StatePanel title="首页工作台" message="正在抓取首页雷达、融资和 Upcoming 数据..." loading /> : null}
      {!data && error ? <StatePanel title="首页工作台" message={error} /> : null}

      {data ? (
        <div className="grid gap-6">
          <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <MetricCard label="BTC 占比" value={`${data.market.btcDominance.toFixed(2)}%`} change={`${data.market.btcDominanceChangePercent >= 0 ? '+' : ''}${data.market.btcDominanceChangePercent.toFixed(2)}%`} />
            <MetricCard label="ETH 占比" value={`${data.market.ethDominance.toFixed(2)}%`} change={`${data.market.ethDominanceChangePercent >= 0 ? '+' : ''}${data.market.ethDominanceChangePercent.toFixed(2)}%`} />
            <MetricCard label="总市值" value={`$${(data.market.totalMarketCap / 1_000_000_000_000).toFixed(2)}T`} change={`${data.market.totalMarketCapChangePercent >= 0 ? '+' : ''}${data.market.totalMarketCapChangePercent.toFixed(2)}%`} />
            <MetricCard label="24h 成交额" value={`$${(data.market.totalVolume24h / 1_000_000_000).toFixed(2)}B`} change={`${data.market.totalVolume24hChangePercent >= 0 ? '+' : ''}${data.market.totalVolume24hChangePercent.toFixed(2)}%`} />
          </section>

          <section className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
            <SurfaceCard title="头部币种" eyebrow="首页首屏">
              <DataTable
                columns={[
                  { key: 'name', label: '币种' },
                  { key: 'price', label: '价格' },
                  { key: 'marketCap', label: '市值' },
                  { key: 'change24h', label: '24h' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.topCoins.map((item) => ({
                  name: `${item.name} (${item.symbol})`,
                  price: item.price,
                  marketCap: item.marketCap,
                  change24h: item.change24h,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>

            <BriefPanel title="今日重点摘要" items={data.brief.length ? data.brief : [
              { title: '首页摘要未生成', description: '刷新后会自动生成今日重点；如果仍为空，说明上游数据需要再试一次。' },
            ]} />
          </section>

          <section className="grid gap-6 xl:grid-cols-2">
            <SurfaceCard title="涨幅榜" eyebrow="Momentum">
              <DataTable
                columns={[
                  { key: 'name', label: '币种' },
                  { key: 'category', label: '类别' },
                  { key: 'change24h', label: '24h' },
                  { key: 'change7d', label: '7d' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.gainers.map((item) => ({
                  name: `${item.name} (${item.symbol})`,
                  category: item.category,
                  change24h: item.change24h,
                  change7d: item.change7d,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>

            <SurfaceCard title="跌幅榜" eyebrow="Risk Radar">
              <DataTable
                columns={[
                  { key: 'name', label: '币种' },
                  { key: 'category', label: '类别' },
                  { key: 'change24h', label: '24h' },
                  { key: 'change7d', label: '7d' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.losers.map((item) => ({
                  name: `${item.name} (${item.symbol})`,
                  category: item.category,
                  change24h: item.change24h,
                  change7d: item.change7d,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>
          </section>

          <section className="grid gap-6 xl:grid-cols-2">
            <SurfaceCard title="即将开始的 IDO / ICO" eyebrow="Upcoming">
              <DataTable
                columns={[
                  { key: 'project', label: '项目' },
                  { key: 'saleType', label: '类型' },
                  { key: 'launchpads', label: '平台' },
                  { key: 'dateLabel', label: '时间' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.upcoming.map((item) => ({
                  project: item.project,
                  saleType: item.saleType,
                  launchpads: item.launchpads,
                  dateLabel: item.dateLabel,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>

            <SurfaceCard title="近期融资" eyebrow="Funding">
              <DataTable
                columns={[
                  { key: 'project', label: '项目' },
                  { key: 'stage', label: '阶段' },
                  { key: 'raise', label: '融资额' },
                  { key: 'funds', label: '机构' },
                  { key: 'links', label: '链接' },
                ]}
                rows={data.funding.map((item) => ({
                  project: item.project,
                  stage: item.stage,
                  raise: item.raise,
                  funds: item.funds,
                  links: <LinkBadges links={item.links} />,
                }))}
              />
            </SurfaceCard>
          </section>
        </div>
      ) : null}
    </AppShell>
  )
}
