import { AppShell } from '@/components/AppShell'
import { BriefPanel } from '@/components/BriefPanel'
import { DataTable } from '@/components/DataTable'
import { LinkBadges } from '@/components/LinkBadges'
import { StatePanel } from '@/components/StatePanel'
import { SurfaceCard } from '@/components/SurfaceCard'
import { useApiResource } from '@/hooks/useApiResource'
import type { OpportunitiesData } from '@shared/cryptorank'

export default function Opportunities() {
  const { data, loading, error, reload } = useApiResource<OpportunitiesData>('/api/opportunities')

  return (
    <AppShell
      title="中文机会页"
      subtitle="把 Upcoming 和空投活动放进同一个页面，用中文告诉你今天先看哪 3 个。"
      onRefresh={reload}
      refreshing={loading}
    >
      {!data && loading ? <StatePanel title="机会页" message="正在抓取 Upcoming 和空投活动..." loading /> : null}
      {!data && error ? <StatePanel title="机会页" message={error} /> : null}

      {data ? (
        <div className="grid gap-6">
          <section className="grid gap-6 xl:grid-cols-[1.08fr_0.92fr]">
            <SurfaceCard title="Upcoming IDO / ICO" eyebrow="Narrative Radar">
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
            <BriefPanel title="今日行动建议" items={data.actions} />
          </section>

          <SurfaceCard title="空投 / 活动雷达" eyebrow="Drop Hunting">
            <DataTable
              columns={[
                { key: 'project', label: '项目' },
                { key: 'activityTypes', label: '类型' },
                { key: 'status', label: '状态' },
                { key: 'updatedLabel', label: '更新' },
                { key: 'links', label: '链接' },
              ]}
              rows={data.airdrops.map((item) => ({
                project: item.project,
                activityTypes: item.activityTypes,
                status: item.status,
                updatedLabel: item.updatedLabel,
                links: <LinkBadges links={item.links} />,
              }))}
            />
          </SurfaceCard>
        </div>
      ) : null}
    </AppShell>
  )
}
