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
    const { dispatch_results, pre_assigned, unassigned: ua, loads } = data

    // Build load map for quick lookup
    const loadMap: Record<number, any> = {}
    for (const l of (loads || [])) {
      if (!loadMap[l.ce_id]) loadMap[l.ce_id] = l
    }

    // Group dispatch results by driver
    const driverMap: Record<number, DriverColumn> = {}
    for (const row of (dispatch_results || [])) {
      if (!driverMap[row.driver_id]) {
        driverMap[row.driver_id] = {
          driver_id: row.driver_id,
          driver_name: row.driver_name,
          board_location: row.board_location,
          loads: [],
        }
      }
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

    // Merge pre-assigned loads (status > 1, already delivered/in-progress)
    // These appear at the top of the driver's column before routed loads.
    const preAssignedCeIds = new Set<number>()
    for (const row of (pre_assigned || [])) {
      preAssignedCeIds.add(row.ce_id)
      if (!driverMap[row.driver_id]) {
        driverMap[row.driver_id] = {
          driver_id: row.driver_id,
          driver_name: row.driver_name,
          board_location: row.board_location,
          loads: [],
        }
      }
      const load = loadMap[row.ce_id]
      driverMap[row.driver_id].loads.unshift({
        ...(load || {}),
        ce_id: row.ce_id,
        site_name: row.site_name || load?.site_name || '',
        site_city: row.site_city || load?.city || '',
        terminal_name: row.terminal_name || load?.terminal_name || '',
        load_status: row.load_status ?? load?.load_status,
        eta: row.eta,
        sequence: undefined,
        pre_assigned: true,
      })
    }

    // Sort loads by sequence within each driver
    for (const col of Object.values(driverMap)) {
      col.loads.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0))
    }

    // Group drivers by board location
    const boardGroups: Record<string, DriverColumn[]> = {}
    for (const loc of BOARD_LOCATIONS) boardGroups[loc] = []
    for (const col of Object.values(driverMap)) {
      const loc = col.board_location || 'TX-AM'
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

    // Only count loads whose delivery_date matches the selected date.
    // The routing engine processes ±1 day, so dispatch_results and
    // unassigned_loads can contain loads from adjacent dates.
    const todayCeIds = new Set(Object.keys(loadMap).map(Number))
    const totalAssigned = Object.values(driverMap).reduce(
      (s, d) => s + d.loads.filter(l => todayCeIds.has(l.ce_id)).length, 0
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

      {/* Unassigned panel */}
      {showUnassigned && unassigned.length > 0 && (
        <div style={{
          padding: '12px 20px',
          background: 'var(--surface-sunken)',
          borderBottom: '1px solid var(--border)',
          maxHeight: '200px',
          overflowY: 'auto',
        }}>
          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '8px', fontFamily: 'var(--font-mono)', textTransform: 'uppercase' }}>
            Unassigned Loads
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {unassigned.map(u => (
              <div key={u.id} style={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                padding: '6px 10px',
                background: 'rgba(239,68,68,0.06)',
                border: '1px solid rgba(239,68,68,0.15)',
                borderRadius: '4px',
                fontSize: '12px',
              }}>
                <span style={{ color: 'var(--text)' }}>{u.site_name} (CE#{u.ce_id})</span>
                <span style={{ color: '#f87171', fontFamily: 'var(--font-mono)' }}>{u.reason}</span>
              </div>
            ))}
          </div>
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
                    <div style={{
                      padding: '10px 12px',
                      borderBottom: '1px solid var(--border)',
                      background: 'var(--surface-overlay)',
                    }}>
                      <div style={{
                        fontWeight: 600,
                        fontSize: '13px',
                        color: 'var(--text)',
                        marginBottom: '2px',
                      }}>
                        {col.driver_name}
                      </div>
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                        {col.loads.length} load{col.loads.length !== 1 ? 's' : ''}
                      </div>
                    </div>

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
