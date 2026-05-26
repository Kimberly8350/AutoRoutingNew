'use client'

import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { format, parseISO } from 'date-fns'

export interface LoadCardData {
  ce_id: number
  customer_name: string
  site_name: string
  site_city: string
  window_start?: string
  window_end?: string
  terminal_name: string
  product_name: string
  load_status: number
  load_status_description: string
  order_number?: string
  eta?: string
  sequence?: number
}

const STATUS_CONFIG: Record<number, { label: string; bg: string; border: string; dot: string }> = {
  1:  { label: 'Unscheduled',      bg: 'rgba(107,114,128,0.1)',  border: 'rgba(107,114,128,0.3)',  dot: '#6b7280' },
  2:  { label: 'Dispatched',       bg: 'rgba(107,114,128,0.1)',  border: 'rgba(107,114,128,0.3)',  dot: '#6b7280' },
  12: { label: 'En Route to Rack', bg: 'rgba(249,115,22,0.1)',   border: 'rgba(249,115,22,0.3)',   dot: '#f97316' },
  20: { label: 'At Rack',          bg: 'rgba(234,179,8,0.1)',    border: 'rgba(234,179,8,0.3)',    dot: '#eab308' },
  22: { label: 'En Route',         bg: 'rgba(29,78,216,0.12)',   border: 'rgba(29,78,216,0.4)',    dot: '#1d4ed8' },
  24: { label: 'At Site',          bg: 'rgba(22,163,74,0.1)',    border: 'rgba(22,163,74,0.3)',    dot: '#16a34a' },
  26: { label: 'Delivered',        bg: 'rgba(13,148,136,0.1)',   border: 'rgba(13,148,136,0.3)',   dot: '#0d9488' },
}

function formatWindow(start?: string, end?: string): string {
  if (!start && !end) return 'Anytime'
  try {
    const s = start ? format(parseISO(start), 'HH:mm') : ''
    const e = end ? format(parseISO(end), 'HH:mm') : ''
    if (s === '00:00' && e === '23:00') return 'Anytime'
    return `${s}–${e}`
  } catch {
    return 'Anytime'
  }
}

function isOverdue(windowEnd?: string): boolean {
  if (!windowEnd) return false
  try {
    const todayStart = new Date()
    todayStart.setHours(0, 0, 0, 0)
    return parseISO(windowEnd) < todayStart
  } catch {
    return false
  }
}

interface Props {
  load: LoadCardData
  isDragging?: boolean
}

export function LoadCard({ load, isDragging }: Props) {
  const status = STATUS_CONFIG[load.load_status] || STATUS_CONFIG[2]

  return (
    <div style={{
      background: isDragging ? 'var(--surface-overlay)' : status.bg,
      border: `1px solid ${isDragging ? 'var(--accent)' : status.border}`,
      borderRadius: '6px',
      padding: '10px 12px',
      cursor: 'grab',
      userSelect: 'none',
      opacity: isDragging ? 0.9 : 1,
      boxShadow: isDragging ? '0 8px 24px rgba(0,0,0,0.5)' : 'none',
      transition: 'box-shadow 0.15s',
      position: 'relative',
    }}>
      {/* Status dot */}
      <div style={{
        position: 'absolute',
        top: '10px',
        right: '10px',
        width: '7px',
        height: '7px',
        borderRadius: '50%',
        background: status.dot,
        boxShadow: `0 0 6px ${status.dot}`,
      }} />

      {/* Sequence badge */}
      {load.sequence !== undefined && (
        <div style={{
          position: 'absolute',
          top: '8px',
          left: '-1px',
          background: 'var(--accent)',
          color: 'white',
          fontSize: '10px',
          fontWeight: 700,
          fontFamily: 'var(--font-mono)',
          padding: '1px 6px',
          borderRadius: '0 3px 3px 0',
        }}>
          {load.sequence + 1}
        </div>
      )}

      <div style={{ paddingTop: load.sequence !== undefined ? '4px' : '0' }}>
        {/* Customer */}
        <div style={{
          fontSize: '11px',
          color: 'var(--text-muted)',
          fontFamily: 'var(--font-mono)',
          marginBottom: '2px',
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
        }}>
          {load.customer_name}
        </div>

        {/* Site name */}
        <div style={{
          fontSize: '13px',
          fontWeight: 600,
          color: 'var(--text)',
          marginBottom: '1px',
          lineHeight: 1.3,
        }}>
          {load.site_name}
        </div>

        {/* City */}
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '8px' }}>
          {load.site_city}
        </div>

        {/* Details grid */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: '4px 8px',
          fontSize: '11px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
            <span>
              <span style={{ color: 'var(--text-dim)' }}>Window: </span>
              <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {formatWindow(load.window_start, load.window_end)}
              </span>
            </span>
            {isOverdue(load.window_end) && (
              <span style={{
                fontSize: '9px',
                fontWeight: 700,
                fontFamily: 'var(--font-mono)',
                color: '#f97316',
                background: 'rgba(249,115,22,0.12)',
                border: '1px solid rgba(249,115,22,0.35)',
                borderRadius: '3px',
                padding: '1px 5px',
                letterSpacing: '0.06em',
                textTransform: 'uppercase',
              }}>
                OVERDUE
              </span>
            )}
          </div>
          <div style={{ gridColumn: '1 / -1' }}>
            <span style={{ color: 'var(--text-dim)' }}>Term: </span>
            <span style={{ color: 'var(--text-muted)' }}>{load.terminal_name || '—'}</span>
          </div>
          <div>
            <span style={{ color: 'var(--text-dim)' }}>Product: </span>
            <span style={{ color: 'var(--text-muted)' }}>{load.product_name}</span>
          </div>
          {load.eta && (
            <div>
              <span style={{ color: 'var(--text-dim)' }}>ETA: </span>
              <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {format(parseISO(load.eta), 'HH:mm')}
              </span>
            </div>
          )}
        </div>

        {/* Bottom row */}
        <div style={{
          marginTop: '8px',
          paddingTop: '8px',
          borderTop: '1px solid rgba(255,255,255,0.06)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <span style={{
            fontSize: '10px',
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-dim)',
          }}>
            CE#{load.ce_id}{load.order_number ? ` · #${load.order_number}` : ''}
          </span>
          <span style={{
            fontSize: '10px',
            fontWeight: 600,
            color: status.dot,
            fontFamily: 'var(--font-mono)',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}>
            {status.label}
          </span>
        </div>
      </div>
    </div>
  )
}

// Sortable wrapper for DnD
export function SortableLoadCard({ load }: { load: LoadCardData }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: `load-${load.ce_id}`,
  })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  }

  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
      <LoadCard load={load} isDragging={isDragging} />
    </div>
  )
}
