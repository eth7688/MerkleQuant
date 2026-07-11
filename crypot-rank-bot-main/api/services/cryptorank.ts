import type {
  AirdropItem,
  BriefData,
  BriefItem,
  CoinItem,
  FundingData,
  FundingItem,
  MarketSnapshot,
  OpportunitiesData,
  RadarData,
  ResourceLink,
  UpcomingItem,
} from '@shared/cryptorank'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import fs from 'node:fs'
import path from 'node:path'

const execFileAsync = promisify(execFile)

type GenericRecord = Record<string, any>

export class CryptoRankError extends Error {}

interface SourceContext {
  source: string[]
}

interface ServiceResult<T> {
  data: T
  source: string[]
}

const valueMap: Record<string, string> = {
  CONFIRMED: '已确认',
  POTENTIAL: '潜在',
  ENDED: '已结束',
  ONGOING: '进行中',
  VERIFICATION: '验证中',
  'Bounty Platform': '赏金平台',
  'Post-IPO Debt': '上市后债务融资',
  'Extended Series B': '扩展 B 轮',
  'M&A': '并购',
  Hold: '持仓',
  Liquidity: '做市',
  Trading: '交易',
  Mainnet: '主网',
  Testnet: '测试网',
  'Mint NFT': '铸造 NFT',
  Social: '社媒',
  Discord: 'Discord',
  Invite: '邀请',
  Quest: '任务',
  Bridge: '跨链',
  Swap: '兑换',
  Deposit: '存款',
  Borrow: '借贷',
  Stake: '质押',
  Wallet: '钱包',
  Download: '下载',
  ANGEL: '天使轮',
  'PRE SEED': 'Pre-Seed',
  SEED: 'Seed',
  STRATEGIC: '战略轮',
  'SERIES A': 'A 轮',
  'SERIES B': 'B 轮',
  'SERIES C': 'C 轮',
  'SERIES D': 'D 轮',
  Grant: 'Grant',
  'Debt Financing': '债务融资',
  'Private Token Sale': '私募轮',
  'Pre-sale': '预售',
  IDO: 'IDO',
  ICO: 'ICO',
  IEO: 'IEO',
}

function readEnvConfig() {
  const envPath = path.join(process.cwd(), '.env')
  let fileContent = ''

  try {
    fileContent = fs.readFileSync(envPath, 'utf8')
  } catch {
    fileContent = ''
  }

  const parsed = Object.fromEntries(
    fileContent
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#') && line.includes('='))
      .map((line) => {
        const index = line.indexOf('=')
        return [line.slice(0, index).trim(), line.slice(index + 1).trim()]
      }),
  )

  return {
    apiKey: parsed.api_key || process.env.api_key || process.env.API_KEY || '',
    baseUrl: parsed.url || process.env.url || process.env.CRYPTORANK_BASE_URL || 'https://api.cryptorank.io/v2',
  }
}

function createSourceContext(): SourceContext {
  return {
    source: [],
  }
}

function appendSource(context: SourceContext, value: string) {
  if (!context.source.includes(value)) {
    context.source.push(value)
  }
}

async function runPythonRaw(mode: 'home' | 'funding' | 'upcoming' | 'airdrops') {
  try {
    const { stdout, stderr } = await execFileAsync('python3', ['scripts/cryptorank_demo.py', 'raw', '--raw-mode', mode], {
      cwd: process.cwd(),
      timeout: 30000,
      maxBuffer: 12 * 1024 * 1024,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    })

    if (stderr?.trim()) {
      throw new CryptoRankError(stderr.trim())
    }

    return JSON.parse(stdout) as GenericRecord
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Python 抓取失败'
    throw new CryptoRankError(message)
  }
}

async function requestOfficial<T>(endpoint: string) {
  const { apiKey, baseUrl } = readEnvConfig()
  if (!apiKey) {
    throw new CryptoRankError('缺少 CryptoRank 官方 API Key')
  }

  const url = `${baseUrl.replace(/\/$/, '')}${endpoint}`
  const response = await fetch(url, {
    headers: {
      'X-Api-Key': apiKey,
      Accept: 'application/json',
      'User-Agent': 'CryptoRank-Demo/1.0',
    },
  })

  const text = await response.text()
  if (!response.ok) {
    throw new CryptoRankError(`官方 API 请求失败: ${response.status} ${text.slice(0, 180)}`)
  }

  return JSON.parse(text) as T
}

function translate(value: unknown) {
  const text = safeText(value)
  return valueMap[text] ?? text
}

function safeText(value: unknown) {
  if (value === undefined || value === null || value === '') {
    return '暂无'
  }
  return String(value).replace(/\s+/g, ' ').trim()
}

function usdValue(value: unknown) {
  if (typeof value === 'number') {
    return value
  }
  if (value && typeof value === 'object' && 'USD' in (value as Record<string, unknown>)) {
    return Number((value as Record<string, unknown>).USD)
  }
  return Number(value ?? 0)
}

function compactNumber(value: unknown) {
  const num = Number(value ?? 0)
  if (!Number.isFinite(num) || num === 0) {
    return '暂无'
  }
  const sign = num < 0 ? '-' : ''
  const abs = Math.abs(num)
  const units = [
    [1_000_000_000_000, 'T'],
    [1_000_000_000, 'B'],
    [1_000_000, 'M'],
    [1_000, 'K'],
  ] as const

  for (const [threshold, suffix] of units) {
    if (abs >= threshold) {
      return `${sign}${(abs / threshold).toFixed(2)}${suffix}`
    }
  }

  if (abs >= 100) {
    return `${sign}${abs.toFixed(0)}`
  }
  if (abs >= 1) {
    return `${sign}${abs.toFixed(2)}`
  }
  return `${sign}${abs.toFixed(4)}`
}

function money(value: unknown) {
  const formatted = compactNumber(value)
  return formatted === '暂无' ? formatted : `$${formatted}`
}

function percent(value: unknown) {
  const num = Number(value ?? 0)
  if (!Number.isFinite(num)) {
    return '暂无'
  }
  return `${num >= 0 ? '+' : ''}${num.toFixed(2)}%`
}

function parseDate(value: unknown) {
  const text = safeText(value).replace(/\s+\([^)]*\)$/, '')
  if (text === '暂无') {
    return null
  }

  const candidates = [text, text.replace(' GMT+0000', 'Z')]
  for (const candidate of candidates) {
    const timestamp = Date.parse(candidate)
    if (!Number.isNaN(timestamp)) {
      return new Date(timestamp)
    }
  }
  return null
}

function relativeDate(value: unknown) {
  const date = parseDate(value)
  if (!date) {
    return safeText(value)
  }

  const diff = date.getTime() - Date.now()
  const days = diff > 0 ? Math.ceil(diff / 86400000) : Math.floor(diff / 86400000)

  if (days === 0) return '今天'
  if (days === 1) return '1天后'
  if (days === -1) return '1天前'
  if (days > 1) return `${days}天后`
  return `${Math.abs(days)}天前`
}

function joinNames(items: unknown[], limit = 3) {
  if (!Array.isArray(items) || !items.length) {
    return [] as string[]
  }

  return items
    .map((item) => {
      if (typeof item === 'string') {
        return translate(item)
      }
      if (item && typeof item === 'object') {
        const record = item as GenericRecord
        return translate(record.name ?? record.label ?? record.symbol)
      }
      return translate(item)
    })
    .filter(Boolean)
    .slice(0, limit)
}

function mapMarket(globalData: GenericRecord): MarketSnapshot {
  return {
    btcDominance: Number(globalData.btcDominance ?? 0),
    btcDominanceChangePercent: Number(globalData.btcDominanceChangePercent ?? 0),
    ethDominance: Number(globalData.ethDominance ?? 0),
    ethDominanceChangePercent: Number(globalData.ethDominanceChangePercent ?? 0),
    totalMarketCap: Number(globalData.totalMarketCap ?? 0),
    totalMarketCapChangePercent: Number(globalData.totalMarketCapChangePercent ?? 0),
    totalVolume24h: Number(globalData.totalVolume24h ?? 0),
    totalVolume24hChangePercent: Number(globalData.totalVolume24hChangePercent ?? 0),
  }
}

function normalizeCategory(value: unknown) {
  if (value && typeof value === 'object') {
    const record = value as GenericRecord
    return translate(record.name ?? record.key ?? record.label ?? record.type)
  }
  return translate(value)
}

function createLink(label: string, url: unknown): ResourceLink | null {
  const text = safeText(url)
  if (text === '暂无') {
    return null
  }
  const absolute = text.startsWith('http://') || text.startsWith('https://')
    ? text
    : text.startsWith('/')
      ? `https://cryptorank.io${text}`
      : `https://${text}`
  return { label, url: absolute }
}

function buildPriceUrl(key: unknown) {
  const text = safeText(key)
  return text === '暂无' ? null : `https://cryptorank.io/price/${text}`
}

function buildFundingUrl(key: unknown) {
  const text = safeText(key)
  return text === '暂无' ? null : `https://cryptorank.io/ico/${text}`
}

function buildCoinLinks(key: unknown) {
  const links = [createLink('CryptoRank', buildPriceUrl(key))]
  return links.filter((link): link is ResourceLink => Boolean(link))
}

function buildFundingLinks(projectKey: unknown, fundKey: unknown) {
  const links = [
    createLink('CryptoRank', buildPriceUrl(projectKey)),
    createLink('融资页', buildFundingUrl(projectKey)),
    createLink('基金页', fundKey ? `https://cryptorank.io/funds/${safeText(fundKey)}/rounds` : null),
  ]
  return links.filter((link): link is ResourceLink => Boolean(link))
}

function buildUpcomingLinks(projectKey: unknown, platformKey: unknown) {
  const links = [
    createLink('CryptoRank', buildPriceUrl(projectKey)),
    createLink('销售页', buildFundingUrl(projectKey)),
    createLink('平台页', buildPriceUrl(platformKey)),
  ]
  return links.filter((link): link is ResourceLink => Boolean(link))
}

function buildAirdropLinks(coinKey: unknown, checkLink: unknown, claimLink: unknown) {
  const links = [
    createLink('CryptoRank', buildPriceUrl(coinKey)),
    createLink('任务页', checkLink),
    createLink('领取页', claimLink),
  ]
  return links.filter((link): link is ResourceLink => Boolean(link))
}

function mapOfficialCoin(item: GenericRecord): CoinItem {
  return {
    name: safeText(item.name),
    symbol: safeText(item.symbol),
    category: normalizeCategory(item.category ?? item.type),
    price: money(item.price),
    marketCap: money(item.marketCap),
    change24h: percent(item.percentChange?.h24 ?? item.percentChange?.['24h']),
    change7d: percent(item.percentChange?.d7 ?? item.percentChange?.['7d']),
    links: buildCoinLinks(item.key ?? item.slug ?? item.symbol),
  }
}

function mapCoin(item: GenericRecord): CoinItem {
  return {
    name: safeText(item.name),
    symbol: safeText(item.symbol),
    category: normalizeCategory(item.category),
    price: money(usdValue(item.price)),
    marketCap: money(usdValue(item.marketCap)),
    change24h: percent(item.percentChange?.['24h'] ?? item.historyPrice?.['24H']),
    change7d: percent(item.historyPrice?.['7D']),
    links: buildCoinLinks(item.key),
  }
}

function mapFundingItem(item: GenericRecord): FundingItem {
  const project = safeText(item.coin?.name ?? item.fund?.name)
  const funds = joinNames(item.funds, 3)
  const signalParts = []

  if (item.raise) signalParts.push(`融资额 ${money(item.raise)}`)
  if (item.valuation) signalParts.push(`估值 ${money(item.valuation)}`)
  if (Array.isArray(item.funds) && item.funds.length > 0) {
    const tiers = item.funds.map((fund: GenericRecord) => fund.tier).filter((tier: unknown) => tier !== null && tier !== undefined)
    if (tiers.length) {
      signalParts.push(`最佳基金层级 T${Math.min(...tiers)}`)
    }
  }

  return {
    project,
    stage: translate(item.type),
    raise: money(item.raise),
    funds,
    dateLabel: relativeDate(item.date),
    signal: signalParts.join(' | ') || '继续跟踪',
    links: buildFundingLinks(item.coin?.key, item.fund?.key),
  }
}

function mapOfficialFundingItem(item: GenericRecord): FundingItem {
  const round = item.fundingRound ?? {}
  const funds = joinNames(item.funds, 3)
  const signalParts = []

  if (round.raise) signalParts.push(`融资额 ${money(round.raise)}`)
  if (round.valuation) signalParts.push(`估值 ${money(round.valuation)}`)
  if (Array.isArray(item.funds) && item.funds.length > 0) {
    const tiers = item.funds.map((fund: GenericRecord) => fund.tier).filter((tier: unknown) => tier !== null && tier !== undefined)
    if (tiers.length) {
      signalParts.push(`最佳基金层级 T${Math.min(...tiers)}`)
    }
  }

  return {
    project: safeText(item.name),
    stage: translate(round.stage),
    raise: money(round.raise),
    funds,
    dateLabel: relativeDate(round.date),
    signal: signalParts.join(' | ') || '继续跟踪',
    links: buildFundingLinks(item.key ?? item.slug, null),
  }
}

function mapUpcomingItem(item: GenericRecord): UpcomingItem {
  const projectName = safeText(item.coin?.name ?? item.name)
  const symbol = safeText(item.coin?.symbol ?? item.symbol)
  return {
    project: symbol !== '暂无' ? `${projectName} (${symbol})` : projectName,
    saleType: Array.isArray(item.type) ? item.type.map((entry: string) => translate(entry)) : [translate(item.type)],
    launchpads: joinNames(item.launchpads ?? [item.platform].filter(Boolean), 3),
    dateLabel: relativeDate(item.when ?? item.date),
    raise: money(item.raise),
    moniScore: compactNumber(item.moniScore),
    links: buildUpcomingLinks(item.coin?.key ?? item.key, item.platform?.key),
  }
}

function mapOfficialSaleItem(item: GenericRecord): UpcomingItem {
  const crowdsale = item.crowdsale ?? item.publicSale ?? item.tokenSale ?? {}
  const launchpads = joinNames(
    crowdsale.launchpads ?? (crowdsale.launchpad ? [crowdsale.launchpad] : []),
    3,
  )
  const saleType = crowdsale.type
    ? [translate(crowdsale.type)]
    : Array.isArray(item.type)
      ? item.type.map((entry: string) => translate(entry))
      : [translate(item.type)]

  return {
    project: `${safeText(item.name)} (${safeText(item.symbol)})`,
    saleType,
    launchpads,
    dateLabel: relativeDate(crowdsale.startDate ?? crowdsale.date ?? item.startDate),
    raise: money(crowdsale.raise ?? item.raise),
    moniScore: compactNumber(item.moniScore ?? item.rank),
    links: buildUpcomingLinks(item.key ?? item.slug, crowdsale.launchpad?.key ?? crowdsale.platform?.key),
  }
}

function mapAirdropItem(item: GenericRecord): AirdropItem {
  const coin = item.coin ?? {}
  return {
    project: `${safeText(coin.name)} (${safeText(coin.symbol)})`,
    rating: compactNumber(item.rating),
    activityTypes: joinNames(item.activityTypes, 3),
    status: translate(item.status),
    updatedLabel: relativeDate(item.statusUpdatedAt),
    funds: joinNames(coin.funds, 3),
    links: buildAirdropLinks(coin.key, item.checkLink, item.linkToClaim),
  }
}

function createBrief(radar: RadarData, airdrops: AirdropItem[]): BriefItem[] {
  const brief: BriefItem[] = []

  const funding = radar.funding[0]
  if (funding) {
    brief.push({
      title: `融资焦点：${funding.project}`,
      description: `${funding.stage}，${funding.raise}，重点机构：${funding.funds.join('、') || '暂无'}。`,
    })
  }

  const upcoming = radar.upcoming[0]
  if (upcoming) {
    brief.push({
      title: `Upcoming 焦点：${upcoming.project}`,
      description: `${upcoming.saleType.join(' / ')}，平台：${upcoming.launchpads.join('、') || '暂无'}，时间：${upcoming.dateLabel}。`,
    })
  }

  const airdrop = airdrops[0]
  if (airdrop) {
    brief.push({
      title: `空投焦点：${airdrop.project}`,
      description: `状态：${airdrop.status}，类型：${airdrop.activityTypes.join('、') || '暂无'}。`,
    })
  }

  brief.push({
    title: '行动建议',
    description: '先看融资里的高层级基金项目，再看 Upcoming 的近期开售，最后从空投活动里挑 1 到 2 个可执行项目。',
  })

  return brief
}

export async function getRadarData(): Promise<ServiceResult<RadarData>> {
  const context = createSourceContext()

  try {
    const [currenciesRes, fundingRes, salesRes] = await Promise.all([
      requestOfficial<{ data: GenericRecord[]; status?: GenericRecord }>('/currencies?limit=12&sortBy=marketCap&sortDirection=DESC&include=percentChange'),
      requestOfficial<{ data: GenericRecord[]; status?: GenericRecord }>('/currencies/funding-rounds?limit=5&sortBy=date&sortDirection=DESC'),
      requestOfficial<{ data: GenericRecord[]; status?: GenericRecord }>('/currencies/public-sales?crowdsaleStatus=upcoming&limit=5&sortBy=startDate&sortDirection=ASC'),
    ])

    appendSource(context, 'https://api.cryptorank.io/v2/currencies')
    appendSource(context, 'https://api.cryptorank.io/v2/currencies/funding-rounds')
    appendSource(context, 'https://api.cryptorank.io/v2/currencies/public-sales')

    const coins = (currenciesRes.data ?? []).map(mapOfficialCoin)
    const topCoins = coins.slice(0, 6)
    const gainers = [...coins]
      .sort((a, b) => Number(b.change24h.replace('%', '')) - Number(a.change24h.replace('%', '')))
      .slice(0, 5)
    const losers = [...coins]
      .sort((a, b) => Number(a.change24h.replace('%', '')) - Number(b.change24h.replace('%', '')))
      .slice(0, 5)
    const funding = (fundingRes.data ?? []).slice(0, 5).map(mapOfficialFundingItem)
    const upcoming = (salesRes.data ?? []).slice(0, 5).map(mapOfficialSaleItem)
    const market = {
      btcDominance: 0,
      btcDominanceChangePercent: 0,
      ethDominance: 0,
      ethDominanceChangePercent: 0,
      totalMarketCap: 0,
      totalMarketCapChangePercent: 0,
      totalVolume24h: 0,
      totalVolume24hChangePercent: 0,
    }
    const brief: BriefItem[] = []

    if (funding[0]) {
      brief.push({
        title: `融资焦点：${funding[0].project}`,
        description: `${funding[0].stage}，${funding[0].raise}，重点机构：${funding[0].funds.join('、') || '暂无'}。`,
      })
    }
    if (upcoming[0]) {
      brief.push({
        title: `Upcoming 焦点：${upcoming[0].project}`,
        description: `${upcoming[0].saleType.join(' / ')}，平台：${upcoming[0].launchpads.join('、') || '暂无'}，时间：${upcoming[0].dateLabel}。`,
      })
    }
    brief.push({
      title: '当前通道',
      description: '当前页已成功走官方 v2 API。',
    })

    return {
      data: { market, topCoins, gainers, losers, upcoming, funding, brief },
      source: context.source,
    }
  } catch (error) {
    appendSource(context, `官方 API 不可用，已回退免费层: ${error instanceof Error ? error.message : 'unknown'}`)
    const page = await runPythonRaw('home')
    const market = mapMarket(page.initData?.globalData ?? {})
    const upcoming = (page.upcomingIco ?? []).slice(0, 5).map(mapUpcomingItem)
    const funding = (page.fallbackRecentFundingRounds ?? []).slice(0, 5).map(mapFundingItem)
    const brief: BriefItem[] = []

    if (funding[0]) {
      brief.push({
        title: `融资焦点：${funding[0].project}`,
        description: `${funding[0].stage}，${funding[0].raise}，重点机构：${funding[0].funds.join('、') || '暂无'}。`,
      })
    }
    if (upcoming[0]) {
      brief.push({
        title: `Upcoming 焦点：${upcoming[0].project}`,
        description: `${upcoming[0].saleType.join(' / ')}，平台：${upcoming[0].launchpads.join('、') || '暂无'}，时间：${upcoming[0].dateLabel}。`,
      })
    }
    brief.push({
      title: '当前通道',
      description: '当前环境下官方 v2 API 被 Cloudflare 拦截，因此已自动回退到免费层抓取。',
    })

    appendSource(context, 'https://cryptorank.io/')
    appendSource(context, 'https://cryptorank.io/upcoming-ico')

    return {
      data: {
        market,
        topCoins: (page.fallbackCoins ?? []).slice(0, 6).map(mapCoin),
        gainers: (page.gainersCoins ?? []).slice(0, 5).map(mapCoin),
        losers: (page.losersCoins ?? []).slice(0, 5).map(mapCoin),
        upcoming,
        funding,
        brief,
      },
      source: context.source,
    }
  }
}

export async function getFundingData(): Promise<ServiceResult<FundingData>> {
  const radar = await getRadarData()
  const highlights = radar.data.funding.slice(0, 3).map((item) => ({
    title: `${item.project} 值得继续跟踪`,
    description: `${item.stage} | ${item.raise} | ${item.signal}`,
  }))

  return {
    data: {
      market: radar.data.market,
      items: radar.data.funding,
      highlights,
    },
    source: radar.source,
  }
}

export async function getUpcomingData(): Promise<ServiceResult<UpcomingItem[]>> {
  try {
    const response = await requestOfficial<{ data: GenericRecord[] }>('/currencies/public-sales?crowdsaleStatus=upcoming&limit=8&sortBy=startDate&sortDirection=ASC')
    return {
      data: (response.data ?? []).slice(0, 8).map(mapOfficialSaleItem),
      source: ['https://api.cryptorank.io/v2/currencies/public-sales'],
    }
  } catch {
    const page = await runPythonRaw('upcoming')
    return {
      data: (page.fallbackRounds?.data ?? []).slice(0, 8).map(mapUpcomingItem),
      source: ['https://cryptorank.io/upcoming-ico'],
    }
  }
}

export async function getAirdropData(): Promise<ServiceResult<AirdropItem[]>> {
  const payload = await runPythonRaw('airdrops')

  return {
    data: (payload.data ?? []).slice(0, 8).map(mapAirdropItem),
    source: ['https://api.cryptorank.io/v0/drop-hunting/activities/table/public'],
  }
}

export async function getOpportunitiesData(): Promise<ServiceResult<OpportunitiesData>> {
  const [upcoming, airdrops] = await Promise.all([getUpcomingData(), getAirdropData()])

  return {
    data: {
      upcoming: upcoming.data,
      airdrops: airdrops.data,
      actions: [
        {
          title: '先看 Upcoming',
          description: upcoming.data[0]
            ? `优先关注 ${upcoming.data[0].project}，它的时间更近，更适合做“今天该看什么”。`
            : '暂无 Upcoming 数据。',
        },
        {
          title: '再筛空投活动',
          description: airdrops.data[0]
            ? `优先看 ${airdrops.data[0].project}，状态为 ${airdrops.data[0].status}。`
            : '暂无空投活动数据。',
        },
        {
          title: '把结果转成内容',
          description: '从融资、Upcoming、空投各挑 1 条，直接拼成一条中文短摘要。',
        },
      ],
    },
    source: [...new Set([...upcoming.source, ...airdrops.source])],
  }
}

export async function getBriefData(): Promise<ServiceResult<BriefData>> {
  const [radar, airdrops] = await Promise.all([getRadarData(), getAirdropData()])
  const items = createBrief(radar.data, airdrops.data)
  radar.data.brief = items
  return {
    data: {
      market: radar.data.market,
      items,
    },
    source: [...new Set([...radar.source, ...airdrops.source])],
  }
}

export function listSources() {
  return []
}

export const internalUtils = {
  translate,
  relativeDate,
  compactNumber,
  money,
  percent,
}
