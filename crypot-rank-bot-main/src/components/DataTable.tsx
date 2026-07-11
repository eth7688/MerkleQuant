import type { ReactNode } from 'react'
import { ArrowUpRight } from 'lucide-react'

interface Column<T> {
  key: keyof T
  label: string
}

type CellValue = ReactNode | ReactNode[]

interface DataTableProps<T extends Record<string, CellValue>> {
  columns: Column<T>[]
  rows: T[]
}

function renderCell(value: CellValue) {
  if (Array.isArray(value)) {
    return value.length ? <div className="flex flex-wrap gap-2">{value}</div> : '暂无'
  }
  return value ?? '暂无'
}

export function DataTable<T extends Record<string, CellValue>>({
  columns,
  rows,
}: DataTableProps<T>) {
  const gridTemplateColumns = `minmax(0, 1.4fr) repeat(${Math.max(columns.length - 1, 0)}, minmax(0, 1fr))`

  return (
    <div className="overflow-hidden rounded-[24px] border border-white/10">
      <div className="grid grid-cols-1 divide-y divide-white/10">
        <div
          className="hidden gap-4 bg-white/5 px-5 py-3 text-xs uppercase tracking-[0.25em] text-zinc-500 md:grid"
          style={{ gridTemplateColumns }}
        >
          {columns.map((column) => (
            <div key={String(column.key)}>{column.label}</div>
          ))}
        </div>

        {rows.map((row, index) => (
          <div
            key={`${String(row[columns[0].key])}-${index}`}
            className="grid gap-3 bg-black/20 px-5 py-4 text-sm text-zinc-200 transition hover:bg-white/5 md:grid"
            style={{ gridTemplateColumns }}
          >
            {columns.map((column) => (
              <div key={String(column.key)} className="flex items-start gap-2">
                <span className="pt-0.5 text-cyan-300 md:hidden">
                  <ArrowUpRight className="h-4 w-4" />
                </span>
                <div>
                  <div className="mb-1 text-[11px] uppercase tracking-[0.25em] text-zinc-500 md:hidden">
                    {column.label}
                  </div>
                  <div>{renderCell(row[column.key])}</div>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
