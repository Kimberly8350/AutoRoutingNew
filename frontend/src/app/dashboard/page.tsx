'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useAuth } from '@/hooks/useAuth'
import { format } from 'date-fns'

// Tab components
import DriversTab from '@/components/tabs/DriversTab'
import TerminalAccessTab from '@/components/tabs/TerminalAccessTab'
import RestrictionsTab from '@/components/tabs/RestrictionsTab'
import LoadsTab from '@/components/tabs/LoadsTab'
import DispatchBoardTab from '@/components/tabs/DispatchBoardTab'
import UsersTab from '@/components/tabs/UsersTab'
import SyncStatus from '@/components/SyncStatus'

const TABS = [
  { id: 'dispatch', label: 'Dispatch Board', icon: '🗂' },
  { id: 'loads', label: 'Loads', icon: '📦' },
  { id: 'drivers', label: 'Drivers', icon: '👤' },
  { id: 'terminals', label: 'Terminal Access', icon: '⛽' },
  { id: 'restrictions', label: 'Restrictions', icon: '🚫' },
  { id: 'users', label: 'Users', icon: '👥', adminOnly: true },
]

export default function DashboardPage() {
  const { user, appUser, loading, signOut } = useAuth()
  const router = useRouter()
  const [activeTab, setActiveTab] = useState('dispatch')
  const [selectedDate, setSelectedDate] = useState(format(new Date(), 'yyyy-MM-dd'))

  useEffect(() => {
    if (!loading && !user) router.push('/')
  }, [user, loading, router])

  if (loading || !user) return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--surface)',
    }}>
      <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: '13px' }}>
        Loading dashboard...
      </div>
    </div>
  )

  const visibleTabs = TABS.filter(t => !t.adminOnly || appUser?.role === 'administrator')

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--surface)' }}>
      {/* Top bar */}
      <header style={{
        height: '56px',
        background: 'var(--surface-raised)',
        borderBottom: '1px solid var(--border)',
        display: 'flex',
        alignItems: 'center',
        padding: '0 20px',
        gap: '16px',
        position: 'sticky',
        top: 0,
        zIndex: 50,
      }}>
        {/* Brand */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginRight: '8px' }}>
          <div style={{
            width: '28px', height: '28px',
            background: 'var(--accent)',
            borderRadius: '6px',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
              <path d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17M17 13v4a2 2 0 01-4 0"/>
            </svg>
          </div>
          <span style={{
            fontFamily: 'var(--font-display)',
            fontWeight: 700,
            fontSize: '16px',
            color: 'var(--text)',
            letterSpacing: '-0.3px',
          }}>
            AutoRoute
          </span>
        </div>

        {/* Date selector */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginLeft: '8px' }}>
          <label style={{ color: 'var(--text-muted)', fontSize: '12px', fontFamily: 'var(--font-mono)' }}>
            Date:
          </label>
          <input
            type="date"
            value={selectedDate}
            onChange={e => setSelectedDate(e.target.value)}
            style={{
              background: 'var(--surface-sunken)',
              border: '1px solid var(--border)',
              borderRadius: '4px',
              color: 'var(--text)',
              padding: '4px 8px',
              fontSize: '13px',
              fontFamily: 'var(--font-mono)',
              cursor: 'pointer',
            }}
          />
          <button
            onClick={() => setSelectedDate(format(new Date(), 'yyyy-MM-dd'))}
            style={{
              padding: '4px 10px',
              background: 'var(--surface-overlay)',
              border: '1px solid var(--border)',
              borderRadius: '4px',
              color: 'var(--text-muted)',
              fontSize: '12px',
              cursor: 'pointer',
              fontFamily: 'var(--font-mono)',
            }}
          >
            Today
          </button>
        </div>

        <div style={{ flex: 1 }} />

        {/* Sync status */}
        <SyncStatus />

        {/* User menu */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <span style={{ color: 'var(--text-muted)', fontSize: '12px', fontFamily: 'var(--font-mono)' }}>
            {appUser?.full_name || user.email}
            {appUser?.role === 'administrator' && (
              <span style={{
                marginLeft: '6px',
                padding: '1px 6px',
                background: 'var(--accent-muted)',
                color: 'var(--accent)',
                borderRadius: '3px',
                fontSize: '10px',
                fontWeight: 600,
              }}>
                ADMIN
              </span>
            )}
          </span>
          <button
            onClick={signOut}
            style={{
              padding: '5px 12px',
              background: 'transparent',
              border: '1px solid var(--border)',
              borderRadius: '4px',
              color: 'var(--text-muted)',
              fontSize: '12px',
              cursor: 'pointer',
              fontFamily: 'var(--font-mono)',
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      {/* Tab nav */}
      <nav style={{
        background: 'var(--surface-raised)',
        borderBottom: '1px solid var(--border)',
        display: 'flex',
        padding: '0 20px',
        gap: '2px',
      }}>
        {visibleTabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              padding: '10px 16px',
              background: 'transparent',
              border: 'none',
              borderBottom: activeTab === tab.id ? '2px solid var(--accent)' : '2px solid transparent',
              color: activeTab === tab.id ? 'var(--text)' : 'var(--text-muted)',
              fontSize: '13px',
              fontWeight: activeTab === tab.id ? 600 : 400,
              cursor: 'pointer',
              fontFamily: 'var(--font-body)',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              transition: 'color 0.15s',
              whiteSpace: 'nowrap',
              marginBottom: '-1px',
            }}
          >
            <span>{tab.icon}</span>
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Tab content */}
      <main style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        {activeTab === 'dispatch' && <DispatchBoardTab selectedDate={selectedDate} />}
        {activeTab === 'loads' && <LoadsTab selectedDate={selectedDate} />}
        {activeTab === 'drivers' && <DriversTab selectedDate={selectedDate} />}
        {activeTab === 'terminals' && <TerminalAccessTab />}
        {activeTab === 'restrictions' && <RestrictionsTab />}
        {activeTab === 'users' && appUser?.role === 'administrator' && <UsersTab />}
      </main>
    </div>
  )
}
