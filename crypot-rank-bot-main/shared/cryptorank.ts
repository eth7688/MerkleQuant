export interface ApiEnvelope<T> {
  updatedAt: string
  source: string[]
  data: T
}

export interface ResourceLink {
  label: string
  url: string
}

export interface MarketSnapshot {
  btcDominance: number
  btcDominanceChangePercent: number
  ethDominance: number
  ethDominanceChangePercent: number
  totalMarketCap: number
  totalMarketCapChangePercent: number
  totalVolume24h: number
  totalVolume24hChangePercent: number
}

export interface CoinItem {
  name: string
  symbol: string
  category: string
  price: string
  marketCap: string
  change24h: string
  change7d: string
  links: ResourceLink[]
}

export interface FundingItem {
  project: string
  stage: string
  raise: string
  funds: string[]
  dateLabel: string
  signal: string
  links: ResourceLink[]
}

export interface UpcomingItem {
  project: string
  saleType: string[]
  launchpads: string[]
  dateLabel: string
  raise: string
  moniScore: string
  links: ResourceLink[]
}

export interface AirdropItem {
  project: string
  rating: string
  activityTypes: string[]
  status: string
  updatedLabel: string
  funds: string[]
  links: ResourceLink[]
}

export interface BriefItem {
  title: string
  description: string
}

export interface RadarData {
  market: MarketSnapshot
  topCoins: CoinItem[]
  gainers: CoinItem[]
  losers: CoinItem[]
  upcoming: UpcomingItem[]
  funding: FundingItem[]
  brief: BriefItem[]
}

export interface FundingData {
  market: MarketSnapshot
  items: FundingItem[]
  highlights: BriefItem[]
}

export interface OpportunitiesData {
  upcoming: UpcomingItem[]
  airdrops: AirdropItem[]
  actions: BriefItem[]
}

export interface BriefData {
  market: MarketSnapshot
  items: BriefItem[]
}
