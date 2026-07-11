import test from 'node:test'
import assert from 'node:assert/strict'
import { internalUtils } from './cryptorank.js'

test('translate can localize common funding and status values', () => {
  assert.equal(internalUtils.translate('SERIES B'), 'B 轮')
  assert.equal(internalUtils.translate('CONFIRMED'), '已确认')
  assert.equal(internalUtils.translate('Unknown Text'), 'Unknown Text')
})

test('compact number and money format readable values', () => {
  assert.equal(internalUtils.compactNumber(1_200_000), '1.20M')
  assert.equal(internalUtils.money(2_500_000_000), '$2.50B')
})

test('relative date returns Chinese human labels', () => {
  const tomorrow = new Date(Date.now() + 86400000).toISOString()
  const yesterday = new Date(Date.now() - 86400000).toISOString()

  assert.equal(internalUtils.relativeDate(tomorrow), '1天后')
  assert.equal(internalUtils.relativeDate(yesterday), '1天前')
})
