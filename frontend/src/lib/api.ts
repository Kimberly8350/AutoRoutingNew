const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

async function getToken(): Promise<string | null> {
  if (typeof window === 'undefined') return null
  const { supabase } = await import('./supabase')
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token || null
}

async function apiFetch(path: string, options: RequestInit = {}) {
  const token = await getToken()
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'API error')
  }
  return res.json()
}

export const api = {
  // Dispatch
  runDispatch: (date: string, reroute = false) =>
    apiFetch('/api/dispatch/run', {
      method: 'POST',
      body: JSON.stringify({ dispatch_date: date, reroute }),
    }),

  getDispatchBoard: (date: string) =>
    apiFetch(`/api/dispatch?dispatch_date=${date}`),

  resequenceLoad: (ceId: number, driverId: number, sequence: number, dispatchDate: string) =>
    apiFetch(`/api/dispatch/${ceId}/resequence`, {
      method: 'PATCH',
      body: JSON.stringify({ driver_id: driverId, sequence, dispatch_date: dispatchDate }),
    }),

  // Loads
  getLoads: (date: string) => apiFetch(`/api/loads?dispatch_date=${date}`),
  addLoad: (load: object) => apiFetch('/api/loads', { method: 'POST', body: JSON.stringify(load) }),
  deleteLoad: (ceId: number) => apiFetch(`/api/loads/${ceId}`, { method: 'DELETE' }),

  // Drivers
  getDrivers: (date: string) => apiFetch(`/api/drivers?dispatch_date=${date}`),
  updateAttendance: (driverId: number, shiftDate: string, attendance: number) =>
    apiFetch(`/api/drivers/${driverId}/attendance`, {
      method: 'PATCH',
      body: JSON.stringify({ shift_date: shiftDate, attendance_expected: attendance }),
    }),

  // Terminal access
  getTerminalAccess: () => apiFetch('/api/terminal-access'),
  addTerminalAccess: (driverId: number, terminalId: number) =>
    apiFetch('/api/terminal-access', {
      method: 'POST',
      body: JSON.stringify({ driver_id: driverId, terminal_id: terminalId }),
    }),
  removeTerminalAccess: (driverId: number, terminalId: number) =>
    apiFetch('/api/terminal-access', {
      method: 'DELETE',
      body: JSON.stringify({ driver_id: driverId, terminal_id: terminalId }),
    }),

  // Restrictions
  getRestrictions: () => apiFetch('/api/restrictions'),
  addRestriction: (data: object) =>
    apiFetch('/api/restrictions', { method: 'POST', body: JSON.stringify(data) }),
  removeRestriction: (id: number) =>
    apiFetch(`/api/restrictions/${id}`, { method: 'DELETE' }),

  // Terminals
  getTerminals: () => apiFetch('/api/terminals'),
  addTerminal: (data: object) =>
    apiFetch('/api/terminals', { method: 'POST', body: JSON.stringify(data) }),
  deleteTerminal: (id: number) =>
    apiFetch(`/api/terminals/${id}`, { method: 'DELETE' }),

  // Sites
  getSites: () => apiFetch('/api/sites'),

  // Sync status
  getSyncStatus: () => apiFetch('/api/sync/status'),
}
