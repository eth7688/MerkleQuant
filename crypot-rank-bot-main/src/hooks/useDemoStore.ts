import { create } from 'zustand'

interface DemoState {
  lastRefreshAt: string | null
  sources: string[]
  setMeta: (updatedAt: string, sources: string[]) => void
}

export const useDemoStore = create<DemoState>((set) => ({
  lastRefreshAt: null,
  sources: [],
  setMeta: (updatedAt, sources) =>
    set({
      lastRefreshAt: updatedAt,
      sources,
    }),
}))
