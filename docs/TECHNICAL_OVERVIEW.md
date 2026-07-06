# AutoRoute — Technical Overview

**Version:** 1.0  
**Stack:** Python 3 · FastAPI · Next.js 14 · Supabase (PostgreSQL) · Google Maps Routes API  
**Deployed on:** Render (API) · Vercel (Frontend) · Supabase Cloud (Database)

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Flow](#2-data-flow)
3. [Database Schema](#3-database-schema)
4. [Sync Pipeline](#4-sync-pipeline)
5. [Backend API](#5-backend-api)
6. [Routing Engine](#6-routing-engine)
7. [Geo & Travel Time](#7-geo--travel-time)
8. [Frontend Application](#8-frontend-application)
9. [Authentication & Authorization](#9-authentication--authorization)
10. [Deployment](#10-deployment)
11. [Setup & First-Time Configuration](#11-setup--first-time-configuration)
12. [Configuration Reference](#12-configuration-reference)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. System Architecture

AutoRoute is a four-layer system. Each layer has a single responsibility and communicates only with its immediate neighbors.

```
┌─────────────────────────────────────────────────────────────────┐
│  SOURCE DATA                                                    │
│  Excel files refreshed by ODBC from the company dispatch system │
└───────────────────────────┬─────────────────────────────────────┘
                            │ sync/sync.py  (runs every 5 min on PC)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  DATABASE                                                       │
│  Supabase (PostgreSQL) — single source of truth                 │
│  12 tables · Row-Level Security · Realtime-capable              │
└───────────┬─────────────────────────────────┬───────────────────┘
            │ supabase-py (service key)        │ supabase-js (anon key)
            ▼                                  ▼
┌───────────────────────┐          ┌───────────────────────────────┐
│  BACKEND API          │◄─────────│  FRONTEND                     │
│  Python FastAPI       │  REST    │  Next.js 14 · TypeScript       │
│  Render (Starter)     │  + JWT   │  Vercel                        │
└───────────────────────┘          └───────────────────────────────┘
```

### Key Design Decisions

- **No microservices.** One API process handles all endpoints. Scaling is done vertically on Render.
- **Supabase as the integration bus.** Both the sync script and the API read/write to the same tables. No message queues or webhooks needed.
- **Stateless API.** The routing engine runs entirely in memory. No job queue, no persistent workers. Each `/api/dispatch/run` call is self-contained.
- **Frontend uses two clients.** Direct Supabase calls (via `supabase-js`) for auth and user management; the FastAPI backend for all business logic. This keeps auth token handling on the Supabase side while keeping routing logic in Python.

---

## 2. Data Flow

### Sync Flow (every 5 minutes)

```
Excel file on disk
  → pandas read_excel()
  → per-table transformer (normalize columns, coerce types, deduplicate)
  → Supabase upsert in 500-row chunks
  → sync_log entry written
```

For `load_details` specifically, two sheets are merged:
- **Main** — order data (ce_id, site, product, gallons, window)
- **Driver & Terminal** — live assignment status (first_name, last_name, terminal_name)

Joined on `ce_id + product_name`. Only loads from the last 60 days are synced.

### Dispatch Flow (on-demand, user-triggered)

```
POST /api/dispatch/run
  → load reference data from Supabase (yards, terminals, sites)
  → load drivers for date (with terminal cards + restrictions)
  → load loads for date ± 1 day
  → clear travel time cache
  → RoutingEngine.run() [thread pool executor]
      → greedy assignment pass
      → retry pass (timing-only failures)
      → 2-opt improvement pass
  → return result to frontend immediately
  → persist to dispatch_results + unassigned_loads [background task]
```

### Board Load Flow (on page load / date change)

```
GET /api/dispatch?dispatch_date=YYYY-MM-DD
  → dispatch_results (engine output for this date)
  → pre_assigned rows (load_status > 1, already in-motion from ODBC)
  → unassigned_loads
  → load_details (paginated, full load metadata)
  → merge + group by driver → return to frontend
```

---

## 3. Database Schema

### Reference Tables

| Table | Primary Key | Description |
|-------|-------------|-------------|
| `yard_locations` | `yard` (text) | Driver home yards with lat/lon |
| `terminal_locations` | `terminal_id` (int) | Fuel loading racks with lat/lon and diesel-wet flag |
| `site_details` | `site_id` (int) | Customer delivery sites with lat/lon and pump-cert flag |

### Driver Tables

| Table | Primary Key | Description |
|-------|-------------|-------------|
| `driver_schedules` | `record_id` (int) | One row per driver per shift date. Includes `attendance_expected` override, start time, board location, yard |
| `driver_terminal_cards` | `(driver_id, terminal_id)` | Which terminals each driver is authorized to load at |
| `driver_restrictions` | `id` (serial) | Per-driver site or customer-group blacklists |

### Load Table

`load_details` — Primary key is `(ce_id, product_name)` because a single order (CE ID) can contain multiple products (e.g. Regular + Diesel in separate compartments).

Key columns: `ce_id`, `delivery_date`, `site_id`, `terminal_id`, `terminal_name`, `product_name`, `gross_gallons`, `load_status`, `window_start`, `window_end`, `delivery_eta`, `first_name`, `last_name` (assigned driver from ODBC).

**Load status values:**

| Value | Meaning | Routable? |
|-------|---------|-----------|
| 1 | Unscheduled | ✓ Engine routes these |
| 2 | Planned/Dispatched | — Pre-assigned display only |
| 12 | En Route to Rack | 🔒 Locked in reroute mode |
| 20 | At Rack | — Pre-assigned display only |
| 22 | En Route to Site | — Pre-assigned display only |
| 24 | At Site | — Pre-assigned display only |
| 26 | Delivered | — Pre-assigned display only |

### Engine Output Tables

| Table | Description |
|-------|-------------|
| `dispatch_results` | One row per stop. Contains driver, sequence, CE ID, ETAs, miles. |
| `unassigned_loads` | One row per unassigned load. Contains failure reason and category. |
| `dispatch_runs` | Audit log. One row per engine run. Only one `is_active=true` per date. |

### Meta Tables

| Table | Description |
|-------|-------------|
| `app_users` | Application user profiles and roles (`user` / `administrator`) |
| `sync_log` | One row per sync run per table. Tracks rows upserted, duration, errors. |

### Row-Level Security

RLS is enabled on `app_users`, `dispatch_results`, `unassigned_loads`, `dispatch_runs`, and `driver_restrictions`. All authenticated users can read dispatch data. Only administrators can manage `app_users`.

---

## 4. Sync Pipeline

**File:** `sync/sync.py`  
**Runtime:** Local Windows PC, scheduled via Task Scheduler or continuous loop (`python sync.py`)  
**Interval:** 300 seconds (configurable via `SYNC_INTERVAL_SECONDS`)

### Excel File Mapping

| Excel File | Supabase Table |
|-----------|---------------|
| `Yard_Locations.xlsx` | `yard_locations` |
| `terminal_locations.xlsx` | `terminal_locations` |
| `site_details.xlsx` | `site_details` |
| `Auto_Routing_Driver_Schedule.xlsx` | `driver_schedules` |
| `Driver_terminal_cards.xlsx` | `driver_terminal_cards` |
| `Loads Feed for Kim.xlsx` (sheet: Main + Driver & Terminal) | `load_details` |

### Transformer Pipeline

Each table has a dedicated transformer function that:
1. Normalizes column names to lowercase snake_case
2. Coerces numeric types (to int/float, dropping unparseable values)
3. Parses dates and datetimes (handles both Excel serial numbers and ISO strings)
4. Applies domain-specific rules (e.g. clamp `pump_certified` to 0/1, derive `board_location` from `division_prefix + default_shift_name`)
5. Drops columns not present in the DB schema
6. Deduplicates on the primary key before upsert

### Upsert Strategy

- Chunks of 500 rows to avoid Supabase request size limits
- `ON CONFLICT` targets: composite key for `driver_terminal_cards` and `load_details`, default (PK) for all others
- `load_details` skips rows with `load_status=1` that exist in Supabase — those are "ready to route" and should not be overwritten by the ODBC pull

### Reliability

Files held open by ODBC are copied to a temp file before reading. The sync log captures per-table status, row counts, and duration. The loop continues on error — one bad table doesn't stop the rest.

---

## 5. Backend API

**File:** `backend/api/main.py`  
**Framework:** FastAPI 0.115 · Python 3.12  
**Auth:** Supabase JWT verified on every request via `Authorization: Bearer <token>`

### Endpoint Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| POST | `/api/dispatch/run` | Run routing engine for a date |
| GET | `/api/dispatch` | Fetch dispatch board (results + pre-assigned + loads) |
| PATCH | `/api/dispatch/{ce_id}/resequence` | Move a load to a different driver/position |
| GET | `/api/loads` | Get all loads for a date |
| POST | `/api/loads` | Add or update a load |
| DELETE | `/api/loads/{ce_id}` | Remove a load |
| GET | `/api/drivers` | Get driver schedules for a date |
| PATCH | `/api/drivers/{driver_id}/attendance` | Toggle driver attendance |
| GET | `/api/terminal-access` | All driver terminal card records |
| POST | `/api/terminal-access` | Grant a driver access to a terminal |
| DELETE | `/api/terminal-access` | Revoke terminal access |
| GET | `/api/restrictions` | All driver restrictions |
| POST | `/api/restrictions` | Add a restriction |
| DELETE | `/api/restrictions/{id}` | Remove a restriction |
| GET | `/api/terminals` | All terminals |
| POST | `/api/terminals` | Add or update a terminal |
| DELETE | `/api/terminals/{id}` | Delete a terminal |
| GET | `/api/sites` | All delivery sites |
| GET | `/api/sync/status` | Last 20 sync log entries |

### Dispatch Run Behavior

`POST /api/dispatch/run` accepts `{ dispatch_date, reroute }`:

- **Normal dispatch** (`reroute: false`): routes all unscheduled loads (status ≤ 1).
- **Reroute** (`reroute: true`): first seeds the engine with any locked loads (status 12, 20) on their assigned drivers, then routes the remainder. Previously active run for the same date is marked inactive.

The engine runs inside `asyncio.run_in_executor` so the FastAPI event loop stays free and the engine's synchronous Google Maps calls work without nested event loop conflicts.

Results are returned immediately to the frontend; persistence to Supabase happens in a `BackgroundTask`.

---

## 6. Routing Engine

**File:** `backend/engine/routing_engine.py`  
**Algorithm:** Greedy assignment + retry pass + 2-opt local search

### Input Data

All reference data is loaded from Supabase before each run:
- `drivers`: active drivers for the date (attendance_expected = 1), with their terminal card sets and restriction sets attached
- `loads`: all loads for dispatch_date ± 1 day (filtered to status ≤ 1 for routing)
- `sites`, `terminals`, `yards`: full reference dictionaries

### Phase 1 — Greedy Assignment

Loads are sorted by priority before the greedy loop:

1. Timed loads first (window_start is not midnight)
2. Earlier delivery date
3. Earlier window start time

For each load, drivers are tried in order of ascending stop count and finish time (lightest first). For each candidate driver, all insertion positions (beginning, middle, end of existing route) are tried. The position producing the best score (`total_empty_miles - total_loaded_miles`) is accepted.

A load is marked unassigned if no driver/position combination produces a valid simulation.

### Phase 2 — Retry Pass

Loads that failed exclusively because of shift time (not hard rules) are retried after the greedy pass completes. Drivers are sorted by lightest workload. This gives timing-only failures a second chance before 2-opt runs.

### Phase 3 — 2-opt Local Search

Iterates over all driver pairs and tries two move types:

- **SWAP**: exchange one load from driver A with one load from driver B
- **MOVE**: relocate one load from driver A to any insertion position in driver B

A move is accepted only if the combined score of both routes improves by more than 0.01 miles (epsilon to avoid float noise). The loop repeats until no improving move is found or 200 iterations are reached.

Locked loads (status 12, 20) are never moved during 2-opt.

### Route Simulation

`_simulate_route` calculates the full timeline for a driver given an ordered list of stops:

```
For each stop:
  1. Drive from current position → terminal (empty miles)
  2. Load service time: 30 min at terminal
  3. Drive from terminal → site (loaded miles)
  4. Check delivery window (±2h early, ±1h late tolerance; reject if >1h late)
  5. Unload service time: 45 min at site
  6. Update current position and time

After last stop:
  7. Drive back to yard (empty miles)
  8. Reject if return time exceeds shift end
```

Travel times come from `get_travel_mins_sync` (Google Maps or haversine fallback, with 20% tanker multiplier applied).

### Hard Constraints

| Constraint | Check Location |
|-----------|---------------|
| Terminal card access | `_check_driver_eligible` |
| Pump certification | `_check_driver_eligible` |
| Site restriction (by site_id) | `_check_driver_eligible` |
| Customer group restriction | `_check_driver_eligible` |
| Diesel-wet sequencing | `_check_diesel_wet_sequence` |
| Shift time / window | `_simulate_route` (returns None = infeasible) |
| Max 4 stops per driver | Greedy loop guard |

### Diesel-Wet Sequencing Rule

A load at a diesel-wet terminal (`is_diesel_wet=1`) can only follow a load whose products are: diesel present, no gasoline, no dyed fuel. This reflects real tanker compartment contamination rules.

### Unassigned Reason Priority

When multiple failure reasons accumulate for a load (one per driver tried), the most informative one is selected using a fixed priority order. Terminal-access and certification failures rank above timing failures, so the displayed reason always reflects the root blocker rather than a cascading symptom.

---

## 7. Geo & Travel Time

**File:** `backend/engine/geo.py`

### Distance

`haversine_miles(lat1, lon1, lat2, lon2)` — great-circle distance in miles using the haversine formula. Earth radius: 3958.8 miles.

### Travel Time

`get_travel_mins_sync` is the primary interface used by the engine. It:

1. Checks the in-process cache (see below)
2. If not cached, opens a fresh event loop and calls `get_travel_mins` (async)
3. `get_travel_mins` calls Google Maps Routes API v2; falls back to haversine at 50 mph if the API fails or is not configured
4. Applies a **20% tanker multiplier** to all results (real-world observation: heavy tankers run ~20% slower than passenger-car estimates)
5. Stores the result in cache

### Caching Strategy

Cache key: `(round(lat1,4), round(lon1,4), round(lat2,4), round(lon2,4), departure_epoch // 900)`

- Coordinates rounded to 4 decimal places (~11 m precision) — same terminal/site always hits the same bucket
- Departure time bucketed into 15-minute slots — drivers leaving at similar times share results
- Cache lives for the lifetime of the process (one dispatch run on Render)
- `clear_travel_cache()` is called before each new dispatch run

### Google Maps Routes API

- **Endpoint:** `https://routes.googleapis.com/directions/v2:computeRoutes`
- **Mode:** DRIVE · TRAFFIC_AWARE · ferries avoided
- **Departure time:** omitted if in the past (Google uses current time for traffic)
- **Field mask:** `routes.duration,routes.distanceMeters` (minimizes response size)
- **Timeout:** 8 seconds per call

A typical dispatch run (15 drivers × 4 stops × 2 legs) produces ~120 unique legs. With caching, repeated terminal→site pairs collapse to ~20–30 real API calls per run — well within Google's free-tier quota.

---

## 8. Frontend Application

**Framework:** Next.js 14 (App Router) · TypeScript · Tailwind CSS  
**Key libraries:** `@dnd-kit` (drag-and-drop) · `date-fns` (date formatting) · `@supabase/supabase-js`

### Page Structure

```
/                   → Login page (page.tsx)
/dashboard          → Main application shell (dashboard/page.tsx)
```

### Dashboard Layout

```
┌─────────────────────────────────────────────────────┐
│ Header: brand · date picker · sync status · user    │
├─────────────────────────────────────────────────────┤
│ Tab nav: Dispatch Board · Loads · Drivers ·         │
│          Terminal Access · Restrictions · Users*    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Active Tab Content                                 │
│                                                     │
└─────────────────────────────────────────────────────┘
* Users tab visible to administrators only
```

### Dispatch Board Tab

The most complex component. Key behaviors:

- Loads the board via `GET /api/dispatch?dispatch_date=` on mount and date change
- Merges engine output (`dispatch_results`) with in-progress loads (`pre_assigned`) — pre-assigned appear first in each driver column
- Groups drivers by board location (TX-AM, TX-PM, FW-AM, FW-PM, ET-AM) into sub-tabs
- **Drag-and-drop resequencing** via `@dnd-kit`: dragging a load card within or between driver columns calls `PATCH /api/dispatch/{ce_id}/resequence`
- **Undo**: board state is snapshot-saved before each Run/Reroute; clicking Undo restores the previous local state
- Unassigned loads panel (expandable) shows reason per load
- Run/Reroute buttons call `POST /api/dispatch/run` and refresh the board on completion

### Load Card

Displays per-load: customer name, site name, city, delivery window, terminal, product, ETA, CE ID, order number, load status (color-coded dot).

Status colors: gray (unscheduled/dispatched) · orange (en route to rack) · yellow (at rack) · blue (en route) · green (at site) · teal (delivered).

### API Client (`lib/api.ts`)

Thin typed wrapper around `fetch`. Attaches a Supabase JWT on every call. Throws on non-2xx responses with the API's `detail` message.

### Auth Flow

1. User submits email/password → `supabase.auth.signInWithPassword`
2. On session, `fetchAppUser` loads the user's row from `app_users` (name, role)
3. All protected routes redirect to `/` if no session
4. `useAuth` hook exposes `user`, `appUser`, `isAdmin`, `signIn`, `signOut`

---

## 9. Authentication & Authorization

### Auth Provider

Supabase Auth (email + password). No OAuth providers configured.

### Token Flow

```
Frontend login → Supabase Auth → JWT (access_token)
     ↓
API calls → Authorization: Bearer <token>
     ↓
FastAPI verify_token → supabase.auth.get_user(token) → user object
```

The backend uses the **anon key** to verify tokens (sufficient for `get_user`). The **service key** is only used for database operations (bypasses RLS).

### Role Model

Two roles stored in `app_users.role`:

| Role | Can do |
|------|--------|
| `user` | View all tabs, run dispatch, modify attendance/restrictions/terminal access |
| `administrator` | All of the above + manage users (activate/deactivate, change roles) |

The Users tab is hidden from non-admins in the frontend and returns "Access denied" if accessed directly.

---

## 10. Deployment

### Infrastructure Overview

| Layer | Service | Plan | Cost |
|-------|---------|------|------|
| Database | Supabase Cloud | Free / Pro | — |
| Backend API | Render Web Service | Starter | ~$7/mo |
| Frontend | Vercel | Free | — |
| Data Sync | Local Windows PC | — | — |
| Source Code | GitHub | Free | — |

### Backend — Render

Defined in `render.yaml`. Service type: `web`, plan: `starter`.

```
Build:        pip install -r backend/requirements.txt
Start:        uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT
Health check: GET /health
Region:       Closest to Texas (Oregon or Ohio recommended)
```

Required environment variables on Render:

| Variable | Notes |
|----------|-------|
| `SUPABASE_URL` | Project URL from Supabase Settings → API |
| `SUPABASE_SERVICE_KEY` | Service role key — keep secret |
| `SUPABASE_ANON_KEY` | Anon/public key |
| `GOOGLE_MAPS_API_KEY` | Restrict to Render server IP in Google Cloud console |
| `FRONTEND_URL` | Set after Vercel deploy (e.g. `https://autorouting.vercel.app`) |

> **Do not use the Render free tier.** Free instances spin down after inactivity, causing multi-second cold starts that will time out dispatch runs. Use Starter ($7/mo) or above.  
> To prevent cold starts entirely, configure an external uptime monitor (e.g. UptimeRobot) to ping `GET /health` every 10 minutes.

After deploying, copy the Render **Deploy Hook URL** from Settings → it can be stored as a GitHub secret (`RENDER_DEPLOY_HOOK_URL`) for CI/CD auto-deploys on push.

### Frontend — Vercel

- **Framework preset:** Next.js
- **Root directory:** `frontend`
- **Branch:** `main`

Required environment variables on Vercel:

| Variable | Notes |
|----------|-------|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Anon key |
| `NEXT_PUBLIC_API_URL` | Render URL (e.g. `https://autorouting-api.onrender.com`) |

After deploying, copy the Vercel URL and set it as `FRONTEND_URL` on Render so CORS is correctly configured.

### Sync Script — Local PC

The sync script runs on a local Windows machine and pushes Excel data to Supabase. It does not need to be accessible from the internet.

**Install dependencies:**
```bash
cd sync
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set SUPABASE_URL, SUPABASE_SERVICE_KEY, EXCEL_DIR
```

**Run once to test:**
```bash
python sync.py --once
```

**Run continuously (every 5 minutes):**
```bash
python sync.py
```

**Schedule via Windows Task Scheduler (recommended for production):**
1. Open Task Scheduler → Create Basic Task
2. Name: `AutoRouting Sync`
3. Trigger: Daily → repeat every 5 minutes for the duration of the day
4. Action: Start a program
   - Program: `python`
   - Arguments: `sync.py --once`
   - Start in: `C:\path\to\your\sync\folder`

### CORS

The API permits origins from `FRONTEND_URL` and `http://localhost:3000`. Both are set at startup. To add additional allowed origins, update the `allow_origins` list in `backend/api/main.py` and redeploy.

---

## 11. Setup & First-Time Configuration

Complete setup order matters — each step depends on the one before it.

### Step 1 — Supabase

1. Log in to [supabase.com](https://supabase.com) → open your project
2. Go to **SQL Editor** → paste the entire contents of `database/schema.sql` → click **Run**
3. Go to **Settings → API** and collect:
   - `Project URL` → `SUPABASE_URL`
   - `anon public` key → `SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_KEY` (treat as a secret)
4. Go to **Authentication → Users** → add your admin user account
5. After the user is created, run this SQL to grant admin role (replace the email):

```sql
INSERT INTO app_users (id, email, full_name, role)
SELECT id, email, 'Your Name', 'administrator'
FROM auth.users
WHERE email = 'your@email.com';
```

### Step 2 — GitHub Repository

Push the codebase to GitHub:

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/AutoRoutingNew.git
git add .
git commit -m "Initial commit: AutoRouting full stack"
git push -u origin main
```

Add the following repository secrets under **Settings → Secrets and Variables → Actions**:

```
SUPABASE_URL
SUPABASE_SERVICE_KEY
SUPABASE_ANON_KEY
NEXT_PUBLIC_SUPABASE_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY
GOOGLE_MAPS_API_KEY
RENDER_DEPLOY_HOOK_URL      (added after Render setup)
NEXT_PUBLIC_API_URL         (added after Render setup)
```

### Step 3 — Google Maps API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Routes API** (required) and **Maps JavaScript API** (optional, for future map views)
3. Create an API key and restrict it to your Render server IP
4. Save the key as `GOOGLE_MAPS_API_KEY`

The system works without this key — it falls back to haversine distance at 50 mph — but route timing accuracy will be significantly lower without real traffic data.

### Step 4 — Render (Backend API)

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repo (`AutoRoutingNew`)
3. Configure the service (settings match `render.yaml`):
   - **Name:** `autorouting-api`
   - **Root Directory:** *(leave blank)*
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Starter
4. Add all required environment variables (see Section 10)
5. Click **Create Web Service** and wait for the first deploy
6. Copy the service URL (e.g. `https://autorouting-api.onrender.com`)
7. Copy the **Deploy Hook URL** from Render Settings → add as `RENDER_DEPLOY_HOOK_URL` GitHub secret

### Step 5 — Vercel (Frontend)

1. Go to [vercel.com](https://vercel.com) → **Add New Project**
2. Import your `AutoRoutingNew` GitHub repo
3. Set **Root Directory** to `frontend`
4. Add environment variables (see Section 10), including `NEXT_PUBLIC_API_URL` = your Render URL
5. Deploy
6. Copy the Vercel URL → go back to Render → add as the `FRONTEND_URL` environment variable and redeploy the backend

### Step 6 — Data Sync (Local PC)

See sync setup instructions in Section 10 (Sync Script — Local PC).

**Excel files required in `EXCEL_DIR`:**

| File | Table Synced |
|------|-------------|
| `Yard_Locations.xlsx` | `yard_locations` |
| `terminal_locations.xlsx` | `terminal_locations` |
| `site_details.xlsx` | `site_details` |
| `Auto_Routing_Driver_Schedule.xlsx` | `driver_schedules` |
| `Driver_terminal_cards.xlsx` | `driver_terminal_cards` |
| `Loads Feed for Kim.xlsx` | `load_details` |

> **Important:** Add the `is_diesel_wet` column (value 0 or 1) to `terminal_locations.xlsx` before the first sync. Terminals missing this column will default to 0 (non-wet).

### Step 7 — First Login & Smoke Test

1. Navigate to your Vercel URL
2. Sign in with your admin email and password
3. Open the **Sync Status** indicator in the header — confirm it shows a recent successful sync
4. Go to **Dispatch Board** → select today's date → click **Run Dispatch**
5. Verify routes appear in the board columns and unassigned count is reasonable

---

## 12. Configuration Reference


### Backend (`backend/.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✓ | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✓ | Service role key (bypasses RLS) |
| `SUPABASE_ANON_KEY` | ✓ | Anon key (used for token verification) |
| `GOOGLE_MAPS_API_KEY` | — | Routes API key. Omit to use haversine-only mode |
| `FRONTEND_URL` | — | Allowed CORS origin. Defaults to `http://localhost:3000` |

### Frontend (`frontend/.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXT_PUBLIC_SUPABASE_URL` | ✓ | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | ✓ | Anon key for Supabase JS client |
| `NEXT_PUBLIC_API_URL` | ✓ | Backend API base URL (Render URL in production) |

### Sync (`sync/.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✓ | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✓ | Service role key |
| `EXCEL_DIR` | ✓ | Path to folder containing Excel files |
| `SYNC_INTERVAL_SECONDS` | — | Sync frequency in seconds. Default: 300 |

### Engine Tuning Constants (`backend/engine/geo.py`, `routing_engine.py`)

| Constant | Default | Description |
|----------|---------|-------------|
| `TANKER_TRAVEL_MULTIPLIER` | 1.20 | Travel time multiplier for tanker trucks vs. passenger-car estimate |
| `TRAVEL_SPEED_MPH` | 50 | Fallback speed when Google Maps is unavailable |
| `LOAD_SERVICE_MINS` | 30 | Time spent loading at terminal |
| `UNLOAD_SERVICE_MINS` | 45 | Time spent unloading at site |
| `EARLY_ALLOWANCE_MINS` | 120 | How early a driver may arrive before a window opens |
| `LATE_ALLOWANCE_MINS` | 60 | Grace period after window closes before warning |
| `REJECT_LATE_MINS` | 60 | Arrival more than this many minutes late = infeasible |
| `MAX_OPT_ITERS` | 200 | Maximum 2-opt iterations before forced stop |

---

## 13. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Sync says "file not found" | `EXCEL_DIR` path incorrect | Check `.env` — use forward slashes even on Windows: `C:/Users/You/Dispatch/` |
| API returns 401 on all requests | Wrong anon key in frontend | Verify `NEXT_PUBLIC_SUPABASE_ANON_KEY` in Vercel environment variables |
| No drivers showing on Dispatch Board | `shift_date` format mismatch in Excel | Confirm `shift_date` parses to `YYYY-MM-DD` after sync; check `sync_log` for errors |
| Driver has no loads assigned | Driver not marked `attendance_expected=1` for that date | Toggle attendance in the Drivers tab, then re-run dispatch |
| All loads unassigned — "No terminal access" | `driver_terminal_cards` table empty or not synced | Run sync once with `python sync.py --once` and check sync_log |
| Google Maps not being used | API key missing or Routes API not enabled | Verify `GOOGLE_MAPS_API_KEY` on Render; confirm Routes API is enabled in Google Cloud Console |
| Dispatch run times out | Render cold start (free tier) | Upgrade to Starter plan; add a health-check ping every 10 min |
| Frontend shows stale board after dispatch run | Browser cache or failed background persist | Refresh the page; check Render logs for persist errors |
| `is_diesel_wet` rule not applying | Column missing from `terminal_locations.xlsx` | Add `is_diesel_wet` column (0 or 1) to the Excel file and re-sync |
| Sync skips locked rows unexpectedly | Rows have `load_status=1` in Supabase from a prior manual edit | This is intentional — status-1 rows are protected from ODBC overwrites |
| `board_location` shows as NULL | `division_prefix` or `default_shift_name` blank in driver schedule Excel | Ensure both columns are populated; valid combos: TX-AM, TX-PM, FW-AM, FW-PM, ET-AM |

### Adding New Users

New users must be invited through Supabase Authentication:

1. Go to **Supabase → Authentication → Users → Invite User**
2. Enter the user's email — they will receive an invite link
3. Once they complete sign-up, their `auth.users` record is created automatically
4. Run this SQL to create their `app_users` profile (or do it from the Users tab in the dashboard):

```sql
INSERT INTO app_users (id, email, full_name, role)
SELECT id, email, 'Full Name Here', 'user'
FROM auth.users
WHERE email = 'newuser@company.com';
```

5. To make them an administrator, change `'user'` to `'administrator'` in the SQL above, or use the role dropdown in the Users tab.

### Future: OneDrive/O365 Sync

The current sync reads Excel files from a local path (`EXCEL_DIR`). When Office 365 permissions are available, the sync script can be updated to pull files directly from a SharePoint/OneDrive URL using the Microsoft Graph API. The transformer pipeline does not need to change — only the file-reading step in `sync_table()` would be updated to fetch from a remote URL instead of a local path.

---

*This document reflects the codebase as of June 2026. For the routing algorithm narrative see `docs/ENGINE_ENHANCEMENTS.md`.*
