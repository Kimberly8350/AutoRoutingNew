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
      borderRadius: '5px',
      padding: '6px 10px',
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
        top: '8px',
        right: '8px',
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: status.dot,
        boxShadow: `0 0 5px ${status.dot}`,
      }} />

      {/* Sequence badge */}
      {load.sequence !== undefined && (
        <div style={{
          position: 'absolute',
          top: '6px',
          left: '-1px',
          background: 'var(--accent)',
          color: 'white',
          fontSize: '9px',
          fontWeight: 700,
          fontFamily: 'var(--font-mono)',
          padding: '1px 5px',
          borderRadius: '0 3px 3px 0',
        }}>
          {load.sequence + 1}
        </div>
      )}

      <div style={{ paddingTop: load.sequence !== undefined ? '3px' : '0' }}>
        {/* Customer · City on one line */}
        <div style={{
          fontSize: '10px',
          color: 'var(--text-dim)',
          fontFamily: 'var(--font-mono)',
          marginBottom: '1px',
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          paddingRight: '14px',
        }}>
          {load.customer_name}{load.site_city ? ` · ${load.site_city}` : ''}
        </div>

        {/* Site name */}
        <div style={{
          fontSize: '12px',
          fontWeight: 600,
          color: 'var(--text)',
          marginBottom: '4px',
          lineHeight: 1.2,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          paddingRight: '14px',
        }}>
          {load.site_name}
        </div>

        {/* Row: Window + OVERDUE + ETA */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '10px', marginBottom: '2px', flexWrap: 'wrap' }}>
          <span>
            <span style={{ color: 'var(--text-dim)' }}>Win: </span>
            <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              {formatWindow(load.window_start, load.window_end)}
            </span>
          </span>
          {isOverdue(load.window_end) && (
            <span style={{
              fontSize: '8px',
              fontWeight: 700,
              fontFamily: 'var(--font-mono)',
              color: '#f97316',
              background: 'rgba(249,115,22,0.12)',
              border: '1px solid rgba(249,115,22,0.35)',
              borderRadius: '3px',
              padding: '1px 4px',
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
            }}>
              OVERDUE
            </span>
          )}
          {load.eta && (
            <span style={{ marginLeft: 'auto' }}>
              <span style={{ color: 'var(--text-dim)' }}>ETA: </span>
              <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {format(parseISO(load.eta), 'HH:mm')}
              </span>
            </span>
          )}
        </div>

        {/* Row: Terminal · Product */}
        <div style={{ fontSize: '10px', color: 'var(--text-muted)', marginBottom: '4px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          <span style={{ color: 'var(--text-dim)' }}>Term: </span>{load.terminal_name || '—'}
          <span style={{ color: 'var(--text-dim)', margin: '0 4px' }}>·</span>
          <span style={{ color: 'var(--text-dim)' }}>Prod: </span>{load.product_name || '—'}
        </div>

        {/* Bottom row */}
        <div style={{
          paddingTop: '4px',
          borderTop: '1px solid rgba(255,255,255,0.05)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: 'var(--text-dim)' }}>
            CE#{load.ce_id}{load.order_number ? ` · #${load.order_number}` : ''}
          </span>
          <span style={{
            fontSize: '9px',
            fontWeight: 600,
            color: status.dot,
            fontFamily: 'var(--font-mono)',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
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
