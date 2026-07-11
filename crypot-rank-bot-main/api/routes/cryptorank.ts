import { Router, type Request, type Response } from 'express'
import {
  CryptoRankError,
  getAirdropData,
  getBriefData,
  getFundingData,
  getOpportunitiesData,
  getRadarData,
  getUpcomingData,
} from '../services/cryptorank.js'

const router = Router()
const cache = new Map<string, unknown>()
const asyncHandler =
  (handler: (req: Request, res: Response) => Promise<unknown>) =>
  (req: Request, res: Response, next: (error?: Error) => void) => {
    Promise.resolve(handler(req, res)).catch(next)
  }

function sendSuccess<T>(res: Response, payload: { data: T; source: string[] }) {
  return res.json({
    updatedAt: new Date().toISOString(),
    source: payload.source,
    data: payload.data,
  })
}

async function withCache<T>(key: string, loader: () => Promise<T>) {
  try {
    const data = await loader()
    cache.set(key, data)
    return data
  } catch (error) {
    if (cache.has(key)) {
      return cache.get(key) as T
    }
    throw error
  }
}

router.get('/radar', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('radar', getRadarData)
  return sendSuccess(res, data)
}))

router.get('/funding', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('funding', getFundingData)
  return sendSuccess(res, data)
}))

router.get('/upcoming', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('upcoming', getUpcomingData)
  return sendSuccess(res, data)
}))

router.get('/airdrops', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('airdrops', getAirdropData)
  return sendSuccess(res, data)
}))

router.get('/opportunities', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('opportunities', getOpportunitiesData)
  return sendSuccess(res, data)
}))

router.get('/brief', asyncHandler(async (_req: Request, res: Response) => {
  const data = await withCache('brief', getBriefData)
  return sendSuccess(res, data)
}))

router.use((error: Error, _req: Request, res: Response, _next: unknown) => {
  const message =
    error instanceof CryptoRankError
      ? error.message
      : '获取 CryptoRank 数据失败，请稍后刷新重试。'

  res.status(500).json({
    updatedAt: new Date().toISOString(),
    source: [],
    error: message,
  })
})

export default router
