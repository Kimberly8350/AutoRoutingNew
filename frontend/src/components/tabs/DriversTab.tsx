'use client'

import { useState, useEffect } from 'react'
import { api } from '@/lib/api'

interface Props { selectedDate: string }

const BOARD_COLORS: Record<string, string> = {
  'TX-AM': '#f97316', 'TX-PM': '#f59e0b',
  'FW-AM': '#3b82f6', 'FW-PM': '#6366f1', 'ET-AM': '#10b981',
}

export default function DriversTab({ selectedDate }: Props) {
  const [drivers, setDrivers] = useState<any[]>([])
  const [inactiveDrivers, setInactiveDrivers] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updating, setUpdating] = useState<number | null>(null)
  const [showInactive, setShowInactive] = useState(false)
  const [confirmDeactivate, setConfirmDeactivate] = useState<any | null>(null)

  useEffect(() => {
    loadDrivers()
    loadInactiveDrivers()
  }, [selectedDate])

  async function loadDrivers() {
    setLoading(true)
    try {
      const data = await api.getDrivers(selectedDate)
      setDrivers(data)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function loadInactiveDrivers() {
    try {
      const data = await api.getInactiveDrivers()
      setInactiveDrivers(data)
    } catch {
      // non-critical
    }
  }

  async function toggleAttendance(driver: any) {
    setUpdating(driver.driver_id)
    const newVal = driver.attendance_expected === 1 ? 0 : 1
    try {
      await api.updateAttendance(driver.driver_id, selectedDate, newVal)
      setDrivers(d => d.map(dr =>
        dr.driver_id === driver.driver_id
          ? { ...dr, attendance_expected: newVal }
          : dr
      ))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setUpdating(null)
    }
  }

  async function deactivateDriver(driver: any) {
    setUpdating(driver.driver_id)
    try {
      await api.deactivateDriver(driver.driver_id, driver.first_name, driver.last_name)
      setDrivers(d => d.filter(dr => dr.driver_id !== driver.driver_id))
      setInactiveDrivers(prev => [...prev, { driver_id: driver.driver_id, first_name: driver.first_name, last_name: driver.last_name }])
    } catch (e: any) {
      setError(e.message)
    } finally {
      setUpdating(null)
      setConfirmDeactivate(null)
    }
  }

  async function reactivateDriver(driver: any) {
    setUpdating(driver.driver_id)
    try {
      await api.reactivateDriver(driver.driver_id)
      setInactiveDrivers(prev => prev.filter(d => d.driver_id !== driver.driver_id))
      await loadDrivers()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setUpdating(null)
    }
  }

  // Group by board location
  const grouped: Record<string, any[]> = {}
  for (const d of drivers) {
    const loc = d.board_location || 'Unknown'
    if (!grouped[loc]) grouped[loc] = []
    grouped[loc].push(d)
  }

  const working = drivers.filter(d => d.attendance_expected === 1)
  const notWorking = drivers.filter(d => d.attendance_expected === 0)

  if (loading) return (
    <div style={{ padding: '40px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
      Loading drivers...
    </div>
  )

  return (
    <div style={{ padding: '20px', overflowY: 'auto', flex: 1 }}>
      {/* Summary */}
      <div style={{ display: 'flex', gap: '12px', marginBottom: '24px' }}>
        {[
          { label: 'Total Drivers', value: drivers.length, color: 'var(--text-muted)' },
          { label: 'Working', value: working.length, color: '#16a34a' },
          { label: 'Not Working', value: notWorking.length, color: '#6b7280' },
        ].map(s => (
          <div key={s.label} style={{
            padding: '14px 20px',
            background: 'var(--surface-raised)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            minWidth: '140px',
          }}>
            <div style={{ fontSize: '24px', fontWeight: 700, color: s.color, fontFamily: 'var(--font-mono)' }}>
              {s.value}
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>{s.label}</div>
          </div>
        ))}
      </div>

      {error && (
        <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '16px' }}>Error: {error}</div>
      )}

      {/* Driver groups */}
      {Object.entries(grouped).map(([loc, drs]) => (
        <div key={loc} style={{ marginBottom: '24px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
            <div style={{
              width: '8px', height: '8px', borderRadius: '50%',
              background: BOARD_COLORS[loc] || '#6b7280',
            }} />
            <span style={{ fontWeight: 600, fontSize: '13px', color: 'var(--text)', fontFamily: 'var(--font-mono)' }}>
              {loc}
            </span>
            <span style={{ color: 'var(--text-dim)', fontSize: '12px' }}>({drs.length} drivers)</span>
          </div>

          <div style={{
            background: 'var(--surface-raised)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            overflow: 'hidden',
          }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ background: 'var(--surface-overlay)' }}>
                  {['Driver', 'Start Time', 'Yard', 'Scheduled', 'Pump Trained', 'Working Today', ''].map(h => (
                    <th key={h} style={{
                      padding: '10px 14px',
                      textAlign: 'left',
                      fontSize: '11px',
                      fontWeight: 600,
                      color: 'var(--text-dim)',
                      textTransform: 'uppercase',
                      letterSpacing: '0.05em',
                      borderBottom: '1px solid var(--border)',
                    }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {drs.sort((a, b) => a.last_name?.localeCompare(b.last_name)).map((d, i) => (
                  <tr key={d.driver_id} style={{
                    background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)',
                    borderBottom: '1px solid var(--border)',
                  }}>
                    <td style={{ padding: '10px 14px', fontSize: '13px', fontWeight: 500, color: 'var(--text)' }}>
                      {d.last_name}, {d.first_name}
                      <span style={{ marginLeft: '8px', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                        #{d.driver_id}
                      </span>
                    </td>
                    <td style={{ padding: '10px 14px', fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-muted)' }}>
                      {d.driver_start_time || '—'}
                    </td>
                    <td style={{ padding: '10px 14px', fontSize: '12px', color: 'var(--text-muted)' }}>
                      {d.yard || '—'}
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      <span style={{
                        padding: '2px 8px',
                        borderRadius: '3px',
                        fontSize: '11px',
                        fontWeight: 600,
                        background: d.driver_schedule ? 'rgba(22,163,74,0.1)' : 'rgba(107,114,128,0.1)',
                        color: d.driver_schedule ? '#16a34a' : '#6b7280',
                      }}>
                        {d.driver_schedule ? 'Yes' : 'Off'}
                      </span>
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      <span style={{
                        padding: '2px 8px',
                        borderRadius: '3px',
                        fontSize: '11px',
                        fontWeight: 600,
                        background: d.pump_trained ? 'rgba(59,130,246,0.1)' : 'transparent',
                        color: d.pump_trained ? '#3b82f6' : 'var(--text-dim)',
                      }}>
                        {d.pump_trained ? 'Yes' : 'No'}
                      </span>
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      <button
                        onClick={() => toggleAttendance(d)}
                        disabled={updating === d.driver_id}
                        style={{
                          padding: '4px 14px',
                          background: d.attendance_expected
                            ? 'rgba(22,163,74,0.15)' : 'rgba(107,114,128,0.1)',
                          border: '1px solid',
                          borderColor: d.attendance_expected
                            ? 'rgba(22,163,74,0.4)' : 'rgba(107,114,128,0.3)',
                          borderRadius: '4px',
                          color: d.attendance_expected ? '#16a34a' : '#6b7280',
                          fontSize: '12px',
                          fontWeight: 600,
                          cursor: updating === d.driver_id ? 'not-allowed' : 'pointer',
                          fontFamily: 'var(--font-body)',
                          opacity: updating === d.driver_id ? 0.6 : 1,
                          transition: 'all 0.15s',
                        }}
                      >
                        {d.attendance_expected ? '✓ Working' : '✗ Not Working'}
                      </button>
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      {confirmDeactivate?.driver_id === d.driver_id ? (
                        <span style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                          <span style={{ fontSize: '11px', color: '#f87171', fontFamily: 'var(--font-mono)' }}>Confirm?</span>
                          <button
                            onClick={() => deactivateDriver(d)}
                            disabled={updating === d.driver_id}
                            style={{ padding: '3px 10px', background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.4)', borderRadius: '4px', color: '#f87171', fontSize: '11px', fontWeight: 600, cursor: 'pointer' }}
                          >Yes</button>
                          <button
                            onClick={() => setConfirmDeactivate(null)}
                            style={{ padding: '3px 10px', background: 'rgba(107,114,128,0.1)', border: '1px solid rgba(107,114,128,0.3)', borderRadius: '4px', color: '#6b7280', fontSize: '11px', cursor: 'pointer' }}
                          >No</button>
                        </span>
                      ) : (
                        <button
                          onClick={() => setConfirmDeactivate(d)}
                          style={{ padding: '3px 10px', background: 'transparent', border: '1px solid rgba(107,114,128,0.25)', borderRadius: '4px', color: 'var(--text-dim)', fontSize: '11px', cursor: 'pointer' }}
                        >
                          Deactivate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {/* Inactive Drivers */}
      <div style={{ marginTop: '32px' }}>
        <button
          onClick={() => setShowInactive(v => !v)}
          style={{
            display: 'flex', alignItems: 'center', gap: '8px',
            background: 'none', border: 'none', cursor: 'pointer',
            color: 'var(--text-dim)', fontSize: '13px', fontFamily: 'var(--font-mono)',
            marginBottom: '10px', padding: 0,
          }}
        >
          <span style={{ fontSize: '10px' }}>{showInactive ? '▾' : '▸'}</span>
          Inactive Drivers ({inactiveDrivers.length})
        </button>

        {showInactive && (
          <div style={{ background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden' }}>
            {inactiveDrivers.length === 0 ? (
              <div style={{ padding: '16px', color: 'var(--text-dim)', fontSize: '12px', fontFamily: 'var(--font-mono)' }}>
                No inactive drivers.
              </div>
            ) : (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ background: 'var(--surface-overlay)' }}>
                    {['Driver', 'ID', ''].map(h => (
                      <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: '11px', fontWeight: 600, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid var(--border)' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {inactiveDrivers.sort((a, b) => (a.last_name || '').localeCompare(b.last_name || '')).map((d, i) => (
                    <tr key={d.driver_id} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)', borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '10px 14px', fontSize: '13px', color: 'var(--text-muted)' }}>
                        {d.last_name}, {d.first_name}
                      </td>
                      <td style={{ padding: '10px 14px', fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-dim)' }}>
                        #{d.driver_id}
                      </td>
                      <td style={{ padding: '10px 14px' }}>
                        <button
                          onClick={() => reactivateDriver(d)}
                          disabled={updating === d.driver_id}
                          style={{ padding: '3px 12px', background: 'rgba(22,163,74,0.1)', border: '1px solid rgba(22,163,74,0.3)', borderRadius: '4px', color: '#16a34a', fontSize: '11px', fontWeight: 600, cursor: 'pointer', opacity: updating === d.driver_id ? 0.6 : 1 }}
                        >
                          Reactivate
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
