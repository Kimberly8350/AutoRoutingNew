'use client'
import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import { useAuth } from '@/hooks/useAuth'

// =================== TERMINAL ACCESS TAB ===================
export function TerminalAccessTab() {
  const [access, setAccess] = useState<any[]>([])
  const [terminals, setTerminals] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [newDriverId, setNewDriverId] = useState('')
  const [newTerminalId, setNewTerminalId] = useState('')
  const [newTerminalName, setNewTerminalName] = useState('')
  const [error, setError] = useState('')
  const [showAddTerminal, setShowAddTerminal] = useState(false)

  useEffect(() => { loadData() }, [])

  async function loadData() {
    setLoading(true)
    try {
      const [a, t] = await Promise.all([api.getTerminalAccess(), api.getTerminals()])
      setAccess(a)
      setTerminals(t)
    } catch (e: any) { setError(e.message) }
    finally { setLoading(false) }
  }

  async function addAccess() {
    if (!newDriverId || !newTerminalId) return
    try {
      await api.addTerminalAccess(parseInt(newDriverId), parseInt(newTerminalId))
      setNewDriverId(''); setNewTerminalId('')
      loadData()
    } catch (e: any) { setError(e.message) }
  }

  async function removeAccess(driverId: number, terminalId: number) {
    try {
      await api.removeTerminalAccess(driverId, terminalId)
      loadData()
    } catch (e: any) { setError(e.message) }
  }

  async function deleteTerminal(id: number) {
    if (!confirm('Delete this terminal?')) return
    try {
      await api.deleteTerminal(id)
      loadData()
    } catch (e: any) { setError(e.message) }
  }

  // Group access by terminal
  const byTerminal: Record<string, any[]> = {}
  for (const a of access) {
    const key = `${a.terminal_id}:${a.terminal_name}`
    if (!byTerminal[key]) byTerminal[key] = []
    byTerminal[key].push(a)
  }

  const filtered = Object.entries(byTerminal).filter(([key]) =>
    !search || key.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div style={{ padding: '20px', overflowY: 'auto', flex: 1 }}>
      {error && <div style={{ color: '#f87171', marginBottom: '16px', fontSize: '13px' }}>Error: {error}</div>}

      {/* Add access */}
      <div style={{
        background: 'var(--surface-raised)', border: '1px solid var(--border)',
        borderRadius: '8px', padding: '16px', marginBottom: '20px',
      }}>
        <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text)', marginBottom: '12px' }}>
          Add Terminal Access
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <input value={newDriverId} onChange={e => setNewDriverId(e.target.value)}
            placeholder="Driver ID" style={inputStyle} />
          <input value={newTerminalId} onChange={e => setNewTerminalId(e.target.value)}
            placeholder="Terminal ID" style={inputStyle} />
          <button onClick={addAccess} style={btnPrimaryStyle}>Add Access</button>
        </div>
      </div>

      {/* Search */}
      <input value={search} onChange={e => setSearch(e.target.value)}
        placeholder="Search terminals..." style={{ ...inputStyle, width: '300px', marginBottom: '16px' }} />

      {/* Terminal list */}
      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Loading...</div>
      ) : filtered.map(([key, drivers]) => {
        const [tid, tname] = key.split(':')
        return (
          <div key={key} style={{
            background: 'var(--surface-raised)', border: '1px solid var(--border)',
            borderRadius: '8px', marginBottom: '12px', overflow: 'hidden',
          }}>
            <div style={{
              padding: '10px 14px',
              background: 'var(--surface-overlay)',
              borderBottom: '1px solid var(--border)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <div>
                <span style={{ fontWeight: 600, color: 'var(--text)', fontSize: '13px' }}>{tname}</span>
                <span style={{ color: 'var(--text-dim)', fontSize: '11px', fontFamily: 'var(--font-mono)', marginLeft: '8px' }}>
                  ID: {tid}
                </span>
              </div>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>{drivers.length} driver(s)</span>
                <button onClick={() => deleteTerminal(parseInt(tid))} style={btnDangerStyle}>Remove Terminal</button>
              </div>
            </div>
            <div style={{ padding: '10px 14px', display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
              {drivers.map(d => (
                <div key={d.driver_id} style={{
                  padding: '4px 10px',
                  background: 'var(--surface-overlay)',
                  border: '1px solid var(--border)',
                  borderRadius: '4px',
                  display: 'flex', alignItems: 'center', gap: '6px',
                  fontSize: '12px', color: 'var(--text)',
                }}>
                  {d.last_name}, {d.first_name}
                  <button
                    onClick={() => removeAccess(d.driver_id, parseInt(tid))}
                    style={{ background: 'none', border: 'none', color: '#f87171', cursor: 'pointer', fontSize: '14px', lineHeight: 1 }}
                  >×</button>
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// =================== RESTRICTIONS TAB ===================
export function RestrictionsTab() {
  const [restrictions, setRestrictions] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [newDriverId, setNewDriverId] = useState('')
  const [newType, setNewType] = useState<'site' | 'customer'>('site')
  const [newSiteId, setNewSiteId] = useState('')
  const [newCustomer, setNewCustomer] = useState('')

  useEffect(() => { loadData() }, [])

  async function loadData() {
    setLoading(true)
    try { setRestrictions(await api.getRestrictions()) }
    catch (e: any) { setError(e.message) }
    finally { setLoading(false) }
  }

  async function addRestriction() {
    if (!newDriverId) return
    try {
      await api.addRestriction({
        driver_id: parseInt(newDriverId),
        restriction_type: newType,
        site_id: newType === 'site' && newSiteId ? parseInt(newSiteId) : null,
        customer_group_name: newType === 'customer' ? newCustomer : null,
      })
      setNewDriverId(''); setNewSiteId(''); setNewCustomer('')
      loadData()
    } catch (e: any) { setError(e.message) }
  }

  async function remove(id: number) {
    try { await api.removeRestriction(id); loadData() }
    catch (e: any) { setError(e.message) }
  }

  return (
    <div style={{ padding: '20px', overflowY: 'auto', flex: 1 }}>
      {error && <div style={{ color: '#f87171', marginBottom: '16px', fontSize: '13px' }}>Error: {error}</div>}

      <div style={{
        background: 'var(--surface-raised)', border: '1px solid var(--border)',
        borderRadius: '8px', padding: '16px', marginBottom: '20px',
      }}>
        <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text)', marginBottom: '12px' }}>
          Add Driver Restriction
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <input value={newDriverId} onChange={e => setNewDriverId(e.target.value)}
            placeholder="Driver ID" style={inputStyle} />
          <select value={newType} onChange={e => setNewType(e.target.value as any)} style={inputStyle}>
            <option value="site">Site</option>
            <option value="customer">Customer Group</option>
          </select>
          {newType === 'site'
            ? <input value={newSiteId} onChange={e => setNewSiteId(e.target.value)}
                placeholder="Site ID" style={inputStyle} />
            : <input value={newCustomer} onChange={e => setNewCustomer(e.target.value)}
                placeholder="Customer Group Name" style={{ ...inputStyle, width: '200px' }} />
          }
          <button onClick={addRestriction} style={btnPrimaryStyle}>Add Restriction</button>
        </div>
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Loading...</div>
      ) : (
        <div style={{
          background: 'var(--surface-raised)', border: '1px solid var(--border)',
          borderRadius: '8px', overflow: 'hidden',
        }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface-overlay)' }}>
                {['Driver ID', 'Type', 'Site ID', 'Customer Group', 'Notes', ''].map(h => (
                  <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: '11px', fontWeight: 600, color: 'var(--text-dim)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {restrictions.map((r, i) => (
                <tr key={r.id} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)', borderBottom: '1px solid var(--border)' }}>
                  <td style={cellStyle}>{r.driver_id}</td>
                  <td style={cellStyle}>
                    <span style={{ padding: '2px 8px', borderRadius: '3px', fontSize: '11px', fontWeight: 600, background: r.restriction_type === 'site' ? 'rgba(249,115,22,0.1)' : 'rgba(99,102,241,0.1)', color: r.restriction_type === 'site' ? '#f97316' : '#818cf8' }}>
                      {r.restriction_type}
                    </span>
                  </td>
                  <td style={cellStyle}>{r.site_id || '—'}</td>
                  <td style={cellStyle}>{r.customer_group_name || '—'}</td>
                  <td style={cellStyle}>{r.notes || '—'}</td>
                  <td style={cellStyle}>
                    <button onClick={() => remove(r.id)} style={btnDangerStyle}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {restrictions.length === 0 && (
            <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-dim)', fontSize: '13px' }}>
              No restrictions configured.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// =================== LOADS TAB ===================
export function LoadsTab({ selectedDate }: { selectedDate: string }) {
  const [loads, setLoads] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'today' | 'prev' | 'next'>('today')

  useEffect(() => { loadData() }, [selectedDate])

  async function loadData() {
    setLoading(true)
    try { setLoads(await api.getLoads(selectedDate)) }
    catch (e: any) { setError(e.message) }
    finally { setLoading(false) }
  }

  async function deleteLoad(ceId: number) {
    if (!confirm(`Remove load CE#${ceId}?`)) return
    try { await api.deleteLoad(ceId); loadData() }
    catch (e: any) { setError(e.message) }
  }

  const filtered = loads.filter(l => {
    if (search) {
      const q = search.toLowerCase()
      if (!String(l.ce_id).includes(q) && !l.site_name?.toLowerCase().includes(q) && !l.order_number?.toLowerCase().includes(q)) return false
    }
    return true
  })

  // Unique loads by ce_id
  const unique = Object.values(filtered.reduce((acc: any, l) => {
    if (!acc[l.ce_id]) acc[l.ce_id] = l
    return acc
  }, {})) as any[]

  return (
    <div style={{ padding: '20px', overflowY: 'auto', flex: 1 }}>
      {error && <div style={{ color: '#f87171', marginBottom: '16px', fontSize: '13px' }}>Error: {error}</div>}

      <div style={{ display: 'flex', gap: '12px', marginBottom: '16px', alignItems: 'center' }}>
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search CE ID, site name, order #..." style={{ ...inputStyle, width: '300px' }} />
        <span style={{ color: 'var(--text-muted)', fontSize: '12px', fontFamily: 'var(--font-mono)' }}>
          {unique.length} loads
        </span>
        <div style={{ flex: 1 }} />
        <button
          onClick={() => { if(confirm('Remove ALL loads for this date?')) api.getLoads(selectedDate).then(() => loadData()) }}
          style={btnDangerStyle}
        >
          Clear All Loads
        </button>
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>Loading loads...</div>
      ) : (
        <div style={{ background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface-overlay)' }}>
                {['CE ID', 'Customer', 'Site', 'City', 'Terminal', 'Products', 'Window', 'Status', 'Driver', ''].map(h => (
                  <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: '11px', fontWeight: 600, color: 'var(--text-dim)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {unique.map((l, i) => (
                <tr key={l.ce_id} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)', borderBottom: '1px solid var(--border)' }}>
                  <td style={{ ...cellStyle, fontFamily: 'var(--font-mono)', fontSize: '11px' }}>{l.ce_id}</td>
                  <td style={cellStyle}>{l.customer_name}</td>
                  <td style={{ ...cellStyle, fontWeight: 500 }}>{l.site_name}</td>
                  <td style={cellStyle}>{l.city}, {l.state}</td>
                  <td style={cellStyle}>{l.terminal_name}</td>
                  <td style={cellStyle}>{l.product_name}</td>
                  <td style={{ ...cellStyle, fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                    {l.window_start && l.window_end ? `${l.window_start?.slice(11,16)}–${l.window_end?.slice(11,16)}` : 'Anytime'}
                  </td>
                  <td style={cellStyle}>
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{l.load_status_description}</span>
                  </td>
                  <td style={cellStyle}>
                    {l.first_name ? `${l.last_name}, ${l.first_name}` : '—'}
                  </td>
                  <td style={cellStyle}>
                    <button onClick={() => deleteLoad(l.ce_id)} style={btnDangerStyle}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {unique.length === 0 && (
            <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-dim)', fontSize: '13px' }}>
              No loads found for {selectedDate}.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// =================== USERS TAB (admin only) ===================
export function UsersTab() {
  const { appUser } = useAuth()
  const [users, setUsers] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const { supabase } = require('@/lib/supabase')

  useEffect(() => { loadUsers() }, [])

  async function loadUsers() {
    setLoading(true)
    try {
      const { data, error } = await supabase.from('app_users').select('*')
      if (error) throw error
      setUsers(data || [])
    } catch (e: any) { setError(e.message) }
    finally { setLoading(false) }
  }

  async function toggleActive(user: any) {
    await supabase.from('app_users').update({ is_active: !user.is_active }).eq('id', user.id)
    loadUsers()
  }

  async function changeRole(user: any, role: string) {
    await supabase.from('app_users').update({ role }).eq('id', user.id)
    loadUsers()
  }

  if (appUser?.role !== 'administrator') return (
    <div style={{ padding: '40px', color: '#f87171', fontFamily: 'var(--font-mono)' }}>Access denied.</div>
  )

  return (
    <div style={{ padding: '20px', overflowY: 'auto', flex: 1 }}>
      {error && <div style={{ color: '#f87171', marginBottom: '16px', fontSize: '13px' }}>Error: {error}</div>}
      <div style={{ marginBottom: '16px', fontSize: '13px', color: 'var(--text-muted)' }}>
        To add new users: invite them via Supabase Auth → they will appear here once registered.
      </div>
      <div style={{ background: 'var(--surface-raised)', border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: 'var(--surface-overlay)' }}>
              {['Name', 'Email', 'Role', 'Active', 'Actions'].map(h => (
                <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: '11px', fontWeight: 600, color: 'var(--text-dim)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)' }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {users.map((u, i) => (
              <tr key={u.id} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)', borderBottom: '1px solid var(--border)' }}>
                <td style={cellStyle}>{u.full_name || '—'}</td>
                <td style={{ ...cellStyle, fontFamily: 'var(--font-mono)', fontSize: '12px' }}>{u.email}</td>
                <td style={cellStyle}>
                  <select
                    value={u.role}
                    onChange={e => changeRole(u, e.target.value)}
                    style={{ background: 'var(--surface-sunken)', border: '1px solid var(--border)', borderRadius: '4px', color: 'var(--text)', padding: '3px 8px', fontSize: '12px', cursor: 'pointer' }}
                  >
                    <option value="user">User</option>
                    <option value="administrator">Administrator</option>
                  </select>
                </td>
                <td style={cellStyle}>
                  <span style={{ color: u.is_active ? '#16a34a' : '#6b7280', fontSize: '12px', fontWeight: 600 }}>
                    {u.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td style={cellStyle}>
                  <button onClick={() => toggleActive(u)} style={u.is_active ? btnDangerStyle : btnPrimaryStyle}>
                    {u.is_active ? 'Deactivate' : 'Activate'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div style={{ padding: '20px', textAlign: 'center', color: 'var(--text-dim)', fontSize: '13px' }}>Loading...</div>}
      </div>
    </div>
  )
}

// Shared styles
const inputStyle: React.CSSProperties = {
  padding: '7px 12px',
  background: 'var(--surface-sunken)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  color: 'var(--text)',
  fontSize: '13px',
  fontFamily: 'var(--font-body)',
  outline: 'none',
  width: '140px',
}

const btnPrimaryStyle: React.CSSProperties = {
  padding: '7px 14px',
  background: 'var(--accent)',
  border: 'none',
  borderRadius: '5px',
  color: 'white',
  fontSize: '12px',
  fontWeight: 600,
  cursor: 'pointer',
  fontFamily: 'var(--font-body)',
}

const btnDangerStyle: React.CSSProperties = {
  padding: '4px 10px',
  background: 'rgba(239,68,68,0.1)',
  border: '1px solid rgba(239,68,68,0.25)',
  borderRadius: '4px',
  color: '#f87171',
  fontSize: '11px',
  fontWeight: 600,
  cursor: 'pointer',
  fontFamily: 'var(--font-body)',
}

const cellStyle: React.CSSProperties = {
  padding: '10px 14px',
  fontSize: '13px',
  color: 'var(--text-muted)',
}
