'use client'

import { useState, useEffect, useCallback } from 'react'
import {
  DndContext, DragEndEvent, DragOverEvent, DragStartEvent,
  DragOverlay, closestCorners, PointerSensor, useSensor, useSensors,
} from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, arrayMove } from '@dnd-kit/sortable'
import { api } from '@/lib/api'
import { LoadCard, SortableLoadCard, LoadCardData } from '@/components/LoadCard'
import { useAuth } from '@/hooks/useAuth'
import { format, parseISO } from 'date-fns'

const BOARD_LOCATIONS = ['TX-AM', 'TX-PM', 'FW-AM', 'FW-PM', 'ET-AM']

interface DriverColumn {
  driver_id: number
  driver_name: string
  board_location: string
  loads: LoadCardData[]
  attendance_expected?: number | null
  driver_schedule?: number | null
  attendance_confirmed?: number | boolean | null
  start_time?: string | null
}

interface Props { selectedDate: string }

export default function DispatchBoardTab({ selectedDate }: Props) {
  const { appUser } = useAuth()
  const [boards, setBoards] = useState<Record<string, DriverColumn[]>>({})
  const [unassigned, setUnassigned] = useState<any[]>([])
  const [totals, setTotals] = useState({ total: 0, assigned: 0, unassigned: 0 })
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [activeLoad, setActiveLoad] = useState<LoadCardData | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [showUnassigned, setShowUnassigned] = useState(false)
  const [showAssigned, setShowAssigned] = useState(false)
  const [validationErrors, setValidationErrors] = useState<string[]>([])
  const [history, setHistory] = useState<any[]>([])  // for undo
  const [activeBoard, setActiveBoard] = useState('TX-AM')

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

  const fetchBoard = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await api.getDispatchBoard(selectedDate)
      buildBoards(data)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [selectedDate])

  useEffect(() => { fetchBoard() }, [fetchBoard])

  function buildBoards(data: any) {
    const { dispatch_results, pre_assigned, unassigned: ua, loads, driver_schedules } = data

    // Build load map for quick lookup
    const loadMap: Record<number, any> = {}
    for (const l of (loads || [])) {
      if (!loadMap[l.ce_id]) loadMap[l.ce_id] = l
    }

    // --- Step 1: Seed ALL scheduled drivers as columns (attendance_expected=1 OR has loads) ---
    // driver_schedules is the source of truth for who appears on the board.
    const driverMap: Record<number, DriverColumn> = {}

    for (const sched of (driver_schedules || [])) {
      const did = sched.driver_id
      if (!did) continue
      const driverName = `${sched.first_name || ''} ${sched.last_name || ''}`.trim()
      driverMap[did] = {
        driver_id: did,
        driver_name: driverName,
        board_location: sched.board_location || 'TX-AM',
        loads: [],
        // Extra schedule metadata for exception display
        attendance_expected: sched.attendance_expected,
        driver_schedule: sched.driver_schedule,
        attendance_confirmed: sched.attendance_confirmed,
        start_time: sched.driver_start_time,
      } as any
    }

    // --- Step 2: Apply dispatch results (routed loads) ---
    // Only add loads to columns already seeded from driver_schedules.
    // Drivers not in the schedule (inactive, off-shift) are skipped — they
    // will not create ghost columns regardless of saved dispatch_results.
    for (const row of (dispatch_results || [])) {
      if (!driverMap[row.driver_id]) continue
      const load = loadMap[row.ce_id]
      if (load) {
        driverMap[row.driver_id].loads.push({
          ...load,
          sequence: row.route_sequence,
          eta: row.eta,
          terminal_name: row.terminal_name || load.terminal_name || '',
          site_city: row.site_city || load.city || load.site_city || '',
        })
      }
    }

    // --- Step 3: Merge pre-assigned loads (status > 1) ---
    // Backend already filters pre_assigned to scheduled drivers only.
    // Skip any that don't match a scheduled column as a safety net.
    for (const row of (pre_assigned || [])) {
      if (!driverMap[row.driver_id]) continue
      const load = loadMap[row.ce_id]
      // Skip duplicates already added via dispatch_results
      if (driverMap[row.driver_id].loads.some((l: any) => l.ce_id === row.ce_id)) continue
      driverMap[row.driver_id].loads.push({
        ...(load || {}),
        ce_id: row.ce_id,
        site_name: row.site_name || load?.site_name || '',
        site_city: row.site_city || load?.city || '',
        terminal_name: row.terminal_name || load?.terminal_name || '',
        load_status: row.load_status ?? load?.load_status,
        eta: row.eta,
        completed_delivery_time: row.completed_delivery_time ?? load?.completed_delivery_time,
        sequence: undefined,
        pre_assigned: true,
      })
    }

    // --- Step 4: Sort loads within each driver column ---
    // Order: Delivered → En Route/At Site → En Route/At Rack → Planned/Assigned → other
    function paStatusGroup(load: any): number {
      const s = Number(load.load_status ?? 0)
      if (s === 26) return 0            // Delivered — done, comes first
      if (s === 22 || s === 24) return 1 // En Route to Site / At Site — delivering now
      if (s === 12 || s === 20) return 2 // En Route to Rack / At Rack — picking up now
      if (s === 10 || s === 2) return 3  // Assigned / Planned — future stops
      return 4
    }
    function paSortTime(load: any): string {
      const s = Number(load.load_status ?? 0)
      if (s === 26) return load.completed_delivery_time || '9999'
      return load.eta || load.delivery_eta || '9999'
    }

    for (const col of Object.values(driverMap)) {
      col.loads.sort((a: any, b: any) => {
        const aPA = a.pre_assigned ? 0 : 1
        const bPA = b.pre_assigned ? 0 : 1
        if (aPA !== bPA) return aPA - bPA
        if (aPA === 0 && bPA === 0) {
          const gDiff = paStatusGroup(a) - paStatusGroup(b)
          if (gDiff !== 0) return gDiff
          return paSortTime(a).localeCompare(paSortTime(b))
        }
        return (a.sequence ?? 0) - (b.sequence ?? 0)
      })
    }

    // --- Step 5: Group by board location ---
    const boardGroups: Record<string, DriverColumn[]> = {}
    for (const loc of BOARD_LOCATIONS) boardGroups[loc] = []

    for (const col of Object.values(driverMap)) {
      const loc = (col as any).board_location || 'TX-AM'
      if (!boardGroups[loc]) boardGroups[loc] = []
      boardGroups[loc].push(col)
    }

    // Sort each board alphabetically by last name
    for (const loc of BOARD_LOCATIONS) {
      boardGroups[loc].sort((a, b) => {
        const aLast = a.driver_name.split(' ').slice(-1)[0]
        const bLast = b.driver_name.split(' ').slice(-1)[0]
        return aLast.localeCompare(bLast)
      })
    }

    setBoards(boardGroups)
    setUnassigned(ua || [])

    const todayCeIds = new Set(Object.keys(loadMap).map(Number))
    const totalAssigned = Object.values(driverMap).reduce(
      (s, d) => s + d.loads.filter((l: any) => todayCeIds.has(l.ce_id)).length, 0
    )
    const todayUnassigned = (ua || []).filter((u: any) => todayCeIds.has(u.ce_id))
    setTotals({
      total: todayCeIds.size,
      assigned: totalAssigned,
      unassigned: todayUnassigned.length,
    })
  }

  async function runDispatch(reroute = false) {
    setRunning(true)
    setError('')
    setValidationErrors([])
    try {
      setHistory(h => [...h, JSON.stringify(boards)])
      const result = await api.runDispatch(selectedDate, reroute)
      setTotals({
        total: result.total_loads,
        assigned: result.assigned_loads,
        unassigned: result.unassigned_loads,
      })
      await fetchBoard()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  function handleUndo() {
    if (history.length === 0) return
    const prev = history[history.length - 1]
    setBoards(JSON.parse(prev))
    setHistory(h => h.slice(0, -1))
  }

  function handleDragStart(event: DragStartEvent) {
    const id = String(event.active.id)
    const ceId = parseInt(id.replace('load-', ''))
    for (const cols of Object.values(boards)) {
      for (const col of cols) {
        const found = col.loads.find(l => l.ce_id === ceId)
        if (found) { setActiveLoad(found); return }
      }
    }
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveLoad(null)
    const { active, over } = event
    if (!over || active.id === over.id) return
    // Find source and destination
    const activeId = String(active.id)
    const overId = String(over.id)
    const activeCeId = parseInt(activeId.replace('load-', ''))
    const overCeId = parseInt(overId.replace('load-', ''))
    // Update local state optimistically
    const newBoards = { ...boards }
    let sourceCol: DriverColumn | null = null
    let destCol: DriverColumn | null = null
    for (const cols of Object.values(newBoards)) {
      for (const col of cols) {
        if (col.loads.some(l => l.ce_id === activeCeId)) sourceCol = col
        if (col.loads.some(l => l.ce_id === overCeId)) destCol = col
      }
    }
    if (!sourceCol || !destCol) return
    if (sourceCol === destCol) {
      const oldIdx = sourceCol.loads.findIndex(l => l.ce_id === activeCeId)
      const newIdx = sourceCol.loads.findIndex(l => l.ce_id === overCeId)
      sourceCol.loads = arrayMove(sourceCol.loads, oldIdx, newIdx)
    } else {
      const load = sourceCol.loads.find(l => l.ce_id === activeCeId)!
      sourceCol.loads = sourceCol.loads.filter(l => l.ce_id !== activeCeId)
      const targetIdx = destCol.loads.findIndex(l => l.ce_id === overCeId)
      destCol.loads.splice(targetIdx, 0, load)
    }
    setBoards(newBoards)
    // Persist to API
    if (destCol) {
      api.resequenceLoad(activeCeId, destCol.driver_id, destCol.loads.findIndex(l => l.ce_id === activeCeId), selectedDate)
        .catch(e => console.error('Resequence failed:', e))
    }
  }

  const filterLoad = (load: LoadCardData) => {
    if (!searchQuery) return true
    const q = searchQuery.toLowerCase()
    return (
      String(load.ce_id).includes(q) ||
      load.site_name?.toLowerCase().includes(q) ||
      load.order_number?.toLowerCase().includes(q) || false
    )
  }

  const currentBoard = boards[activeBoard] || []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Action bar */}
      <div style={{
        padding: '12px 20px',
        background: 'var(--surface-raised)',
        borderBottom: '1px solid var(--border)',
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        flexWrap: 'wrap',
      }}>
        {/* Run/Reroute/Undo buttons */}
        <button
          onClick={() => runDispatch(false)}
          disabled={running}
          style={{
            padding: '8px 18px',
            background: 'var(--accent)',
            border: 'none',
            borderRadius: '6px',
            color: 'white',
            fontWeight: 600,
            fontSize: '13px',
            cursor: running ? 'not-allowed' : 'pointer',
            opacity: running ? 0.7 : 1,
            fontFamily: 'var(--font-body)',
            display: 'flex', alignItems: 'center', gap: '6px',
          }}
        >
          {running ? '⏳ Running...' : '▶ Run Dispatch'}
        </button>

        <button
          onClick={() => runDispatch(true)}
          disabled={running}
          style={{
            padding: '8px 18px',
            background: 'var(--surface-overlay)',
            border: '1px solid var(--border-strong)',
            borderRadius: '6px',
            color: 'var(--text)',
            fontWeight: 600,
            fontSize: '13px',
            cursor: running ? 'not-allowed' : 'pointer',
            fontFamily: 'var(--font-body)',
          }}
        >
          🔄 Reroute
        </button>

        <button
          onClick={handleUndo}
          disabled={history.length === 0}
          style={{
            padding: '8px 14px',
            background: 'var(--surface-overlay)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: history.length === 0 ? 'var(--text-dim)' : 'var(--text-muted)',
            fontSize: '13px',
            cursor: history.length === 0 ? 'not-allowed' : 'pointer',
            fontFamily: 'var(--font-body)',
          }}
        >
          ↩ Undo
        </button>

        <div style={{ flex: 1 }} />

        {/* Search */}
        <input
          type="text"
          placeholder="Search CE ID, site, order #..."
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          style={{
            padding: '7px 12px',
            background: 'var(--surface-sunken)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            color: 'var(--text)',
            fontSize: '13px',
            fontFamily: 'var(--font-mono)',
            width: '240px',
            outline: 'none',
          }}
        />

        {/* Totals */}
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => setShowAssigned(!showAssigned)}
            style={{
              padding: '6px 12px',
              background: 'rgba(22,163,74,0.1)',
              border: '1px solid rgba(22,163,74,0.3)',
              borderRadius: '5px',
              color: '#16a34a',
              fontSize: '12px',
              fontWeight: 600,
              fontFamily: 'var(--font-mono)',
              cursor: 'pointer',
            }}
          >
            ✓ {totals.assigned} Assigned
          </button>
          <button
            onClick={() => setShowUnassigned(!showUnassigned)}
            style={{
              padding: '6px 12px',
              background: 'rgba(239,68,68,0.1)',
              border: '1px solid rgba(239,68,68,0.3)',
              borderRadius: '5px',
              color: '#f87171',
              fontSize: '12px',
              fontWeight: 600,
              fontFamily: 'var(--font-mono)',
              cursor: 'pointer',
            }}
          >
            ✗ {totals.unassigned} Unassigned
          </button>
          <div style={{
            padding: '6px 12px',
            background: 'var(--surface-overlay)',
            border: '1px solid var(--border)',
            borderRadius: '5px',
            color: 'var(--text-muted)',
            fontSize: '12px',
            fontFamily: 'var(--font-mono)',
          }}>
            Total: {totals.total}
          </div>
        </div>
      </div>

      {/* Validation errors */}
      {validationErrors.length > 0 && (
        <div style={{
          padding: '10px 20px',
          background: 'rgba(239,68,68,0.08)',
          borderBottom: '1px solid rgba(239,68,68,0.2)',
        }}>
          {validationErrors.map((e, i) => (
            <div key={i} style={{ color: '#f87171', fontSize: '12px', fontFamily: 'var(--font-mono)' }}>⚠ {e}</div>
          ))}
        </div>
      )}

      {error && (
        <div style={{
          padding: '10px 20px',
          background: 'rgba(239,68,68,0.08)',
          borderBottom: '1px solid rgba(239,68,68,0.2)',
          color: '#f87171', fontSize: '13px',
        }}>
          Error: {error}
        </div>
      )}

      {/* Assigned loads list panel */}
      {showAssigned && (
        <div style={{
          padding: '12px 20px',
          background: 'var(--surface-sunken)',
          borderBottom: '1px solid var(--border)',
          maxHeight: '260px',
          overflowY: 'auto',
        }}>
          <div style={{ fontSize: '12px', fontWeight: 600, color: '#16a34a', marginBottom: '8px', fontFamily: 'var(--font-mono)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Assigned Loads — {totals.assigned} total
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
            <thead>
              <tr style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
                <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>CE ID</th>
                <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Driver</th>
                <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Terminal</th>
                <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Site</th>
                <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>City</th>
                <th style={{ textAlign: 'right', padding: '4px 8px', fontWeight: 500 }}>Seq</th>
              </tr>
            </thead>
            <tbody>
              {Object.values(boards).flat().flatMap(col =>
                col.loads
                  .filter((l: any) => !l.pre_assigned)
                  .map((load: any) => ({ load, driver: col.driver_name, board: col.board_location }))
              ).sort((a, b) => {
                if (a.board !== b.board) return a.board.localeCompare(b.board)
                return a.driver.localeCompare(b.driver)
              }).map(({ load, driver }) => (
                <tr key={load.ce_id} style={{ borderTop: '1px solid var(--border)', color: 'var(--text)' }}>
                  <td style={{ padding: '5px 8px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>{load.ce_id}</td>
                  <td style={{ padding: '5px 8px', fontWeight: 500 }}>{driver}</td>
                  <td style={{ padding: '5px 8px', color: 'var(--text-muted)' }}>{load.terminal_name || '—'}</td>
                  <td style={{ padding: '5px 8px' }}>{load.site_name || '—'}</td>
                  <td style={{ padding: '5px 8px', color: 'var(--text-muted)' }}>{load.site_city || load.city || '—'}</td>
                  <td style={{ padding: '5px 8px', textAlign: 'right', fontFamily: 'var(--font-mono)', color: 'var(--text-dim)' }}>{load.sequence ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {totals.assigned === 0 && (
            <div style={{ color: 'var(--text-dim)', fontSize: '12px', fontFamily: 'var(--font-mono)', padding: '12px 0' }}>
              No assigned loads. Run Dispatch first.
            </div>
          )}
        </div>
      )}

      {/* Unassigned loads list panel */}
      {showUnassigned && (
        <div style={{
          padding: '12px 20px',
          background: 'var(--surface-sunken)',
          borderBottom: '1px solid var(--border)',
          maxHeight: '260px',
          overflowY: 'auto',
        }}>
          <div style={{ fontSize: '12px', fontWeight: 600, color: '#f87171', marginBottom: '8px', fontFamily: 'var(--font-mono)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Unassigned Loads — {totals.unassigned} total
          </div>
          {unassigned.length === 0 ? (
            <div style={{ color: 'var(--text-dim)', fontSize: '12px', fontFamily: 'var(--font-mono)', padding: '8px 0' }}>
              No unassigned loads. Run Dispatch to see results.
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
              <thead>
                <tr style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>CE ID</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Site</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Terminal</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 500 }}>Reason</th>
                </tr>
              </thead>
              <tbody>
                {unassigned.map(u => (
                  <tr key={u.id ?? u.ce_id} style={{ borderTop: '1px solid var(--border)', color: 'var(--text)' }}>
                    <td style={{ padding: '5px 8px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>{u.ce_id}</td>
                    <td style={{ padding: '5px 8px' }}>{u.site_name || '—'}</td>
                    <td style={{ padding: '5px 8px', color: 'var(--text-muted)' }}>{u.terminal_name || '—'}</td>
                    <td style={{ padding: '5px 8px', color: '#f87171', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>{u.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Board location tabs */}
      <div style={{
        display: 'flex',
        padding: '8px 20px 0',
        background: 'var(--surface)',
        gap: '4px',
        borderBottom: '1px solid var(--border)',
      }}>
        {BOARD_LOCATIONS.map(loc => (
          <button
            key={loc}
            onClick={() => setActiveBoard(loc)}
            style={{
              padding: '6px 14px',
              background: activeBoard === loc ? 'var(--surface-raised)' : 'transparent',
              border: '1px solid',
              borderColor: activeBoard === loc ? 'var(--border-strong)' : 'transparent',
              borderBottom: activeBoard === loc ? '1px solid var(--surface-raised)' : '1px solid transparent',
              borderRadius: '6px 6px 0 0',
              color: activeBoard === loc ? 'var(--accent)' : 'var(--text-muted)',
              fontSize: '12px',
              fontWeight: activeBoard === loc ? 600 : 400,
              cursor: 'pointer',
              fontFamily: 'var(--font-mono)',
              marginBottom: '-1px',
            }}
          >
            {loc}
            <span style={{
              marginLeft: '6px',
              fontSize: '10px',
              color: activeBoard === loc ? 'var(--accent-dim)' : 'var(--text-dim)',
            }}>
              ({(boards[loc] || []).length})
            </span>
          </button>
        ))}
      </div>

      {/* Board */}
      <div style={{ flex: 1, overflowX: 'auto', overflowY: 'hidden', padding: '16px 20px' }}>
        {loading ? (
          <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', padding: '20px' }}>
            Loading dispatch board...
          </div>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCorners}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <div style={{
              display: 'flex',
              gap: '12px',
              height: 'calc(100vh - 260px)',
              minWidth: 'max-content',
            }}>
              {currentBoard.map(col => {
                const filteredLoads = col.loads.filter(filterLoad)
                return (
                  <div key={col.driver_id} style={{
                    width: '240px',
                    flexShrink: 0,
                    display: 'flex',
                    flexDirection: 'column',
                    background: 'var(--surface-raised)',
                    border: '1px solid var(--border)',
                    borderRadius: '8px',
                    overflow: 'hidden',
                  }}>
                    {/* Driver header */}
                    {(() => {
                      const sched = col as any
                      // attendance_expected===null means driver has loads but wasn't
                      // on the schedule — flag as an exception (like the * in legacy system)
                      const isException = sched.attendance_expected === null
                      return (
                        <div style={{
                          padding: '10px 12px',
                          borderBottom: '1px solid var(--border)',
                          background: isException
                            ? 'rgba(245,158,11,0.08)'
                            : 'var(--surface-overlay)',
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '5px', flexWrap: 'wrap', marginBottom: '3px' }}>
                            <span style={{ fontWeight: 600, fontSize: '13px', color: 'var(--text)' }}>
                              {isException ? '* ' : ''}{col.driver_name}
                            </span>
                            {isException && (
                              <span style={{
                                fontSize: '10px', fontWeight: 700,
                                padding: '1px 5px', borderRadius: '3px',
                                background: 'rgba(245,158,11,0.15)', color: '#f59e0b',
                              }}>EXTRA</span>
                            )}
                          </div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                            {col.loads.length} load{col.loads.length !== 1 ? 's' : ''}
                            {sched.start_time && (
                              <span style={{ marginLeft: '6px', color: 'var(--text-dim)' }}>
                                · {sched.start_time}
                              </span>
                            )}
                          </div>
                        </div>
                      )
                    })()}

                    {/* Load list */}
                    <div style={{
                      flex: 1,
                      overflowY: 'auto',
                      padding: '8px',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '6px',
                    }}>
                      <SortableContext
                        items={filteredLoads.map(l => `load-${l.ce_id}`)}
                        strategy={verticalListSortingStrategy}
                      >
                        {filteredLoads.map(load => (
                          <SortableLoadCard key={load.ce_id} load={load} />
                        ))}
                      </SortableContext>
                      {filteredLoads.length === 0 && (
                        <div style={{
                          color: 'var(--text-dim)',
                          fontSize: '12px',
                          fontFamily: 'var(--font-mono)',
                          textAlign: 'center',
                          padding: '20px 0',
                        }}>
                          No loads
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}

              {currentBoard.length === 0 && (
                <div style={{
                  color: 'var(--text-muted)',
                  fontFamily: 'var(--font-mono)',
                  fontSize: '13px',
                  padding: '40px',
                  textAlign: 'center',
                  width: '100%',
                }}>
                  No drivers on {activeBoard} board for {selectedDate}.<br />
                  <span style={{ color: 'var(--text-dim)', fontSize: '12px' }}>
                    Run Dispatch to generate assignments.
                  </span>
                </div>
              )}
            </div>

            <DragOverlay>
              {activeLoad && <LoadCard load={activeLoad} isDragging />}
            </DragOverlay>
          </DndContext>
        )}
      </div>
    </div>
  )
}
