'use client'
import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import { formatDistanceToNow, parseISO } from 'date-fns'

export default function SyncStatus() {
  const [lastSync, setLastSync] = useState<any>(null)
  const [, setTick] = useState(0)

  useEffect(() => {
    fetchStatus()
    const iv = setInterval(() => {
      fetchStatus()
      setTick(t => t + 1)
    }, 60000) // refresh every minute
    return () => clearInterval(iv)
  }, [])

  async function fetchStatus() {
    try {
      const rows = await api.getSyncStatus()
      if (rows?.length) setLastSync(rows[0])
    } catch {}
  }

  if (!lastSync) return null

  const isOk = lastSync.status === 'success'
  const ago = lastSync.synced_at
    ? formatDistanceToNow(parseISO(lastSync.synced_at), { addSuffix: true })
    : ''

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: '6px',
      padding: '4px 10px',
      background: isOk ? 'rgba(22,163,74,0.08)' : 'rgba(239,68,68,0.08)',
      border: '1px solid',
      borderColor: isOk ? 'rgba(22,163,74,0.2)' : 'rgba(239,68,68,0.2)',
      borderRadius: '4px',
      fontSize: '11px',
      fontFamily: 'var(--font-mono)',
      color: isOk ? '#16a34a' : '#f87171',
    }}>
      <div style={{
        width: '6px', height: '6px', borderRadius: '50%',
        background: isOk ? '#16a34a' : '#f87171',
        animation: isOk ? 'pulseSoft 2s ease-in-out infinite' : 'none',
      }} />
      <span>Synced {ago}</span>
    </div>
  )
}
