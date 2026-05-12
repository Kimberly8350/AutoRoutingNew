# AutoRouting Dashboard — Complete Setup Guide

## What You're Getting

| Layer | Technology | Hosted On |
|-------|-----------|-----------|
| Database | Supabase (PostgreSQL) | Supabase Cloud |
| Routing Engine API | Python FastAPI | Render (paid) |
| Frontend Dashboard | Next.js (React) | Vercel (free) |
| Data Sync | Python script | Your PC (runs in background) |
| Source Code | Git | GitHub: AutoRoutingNew |

---

## Step 1 — Supabase Setup

1. Log in to [supabase.com](https://supabase.com) → open your project
2. Go to **SQL Editor** → paste the entire contents of `database/schema.sql` → click **Run**
3. Go to **Settings → API** and copy:
   - `Project URL` → this is your `SUPABASE_URL`
   - `anon public` key → this is your `SUPABASE_ANON_KEY`
   - `service_role` key → this is your `SUPABASE_SERVICE_KEY` (keep this secret!)

4. In Supabase → **Authentication → Email** → enable "Confirm email" if you want, or disable for easier setup
5. Go to **Authentication → Users** → add yourself as the first admin user
6. After your user is created, run this SQL to make yourself admin (replace the email):
   ```sql
   INSERT INTO app_users (id, email, full_name, role)
   SELECT id, email, 'Your Name', 'administrator'
   FROM auth.users
   WHERE email = 'your@email.com';
   ```

---

## Step 2 — GitHub Repository Setup

1. Go to your **AutoRoutingNew** repo on GitHub
2. Push all code from this project into the repo:
   ```bash
   git init
   git remote add origin https://github.com/YOUR_USERNAME/AutoRoutingNew.git
   git add .
   git commit -m "Initial commit: AutoRouting full stack"
   git push -u origin main
   ```

3. In GitHub → **Settings → Secrets and Variables → Actions**, add these secrets:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `SUPABASE_ANON_KEY`
   - `NEXT_PUBLIC_SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `GOOGLE_MAPS_API_KEY`
   - `RENDER_DEPLOY_HOOK_URL` (added in Step 3)
   - `VERCEL_TOKEN` (added in Step 4)
   - `NEXT_PUBLIC_API_URL` (your Render URL, added after Step 3)

---

## Step 3 — Google Maps API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Enable these APIs:
   - **Routes API** (for routing with traffic)
   - **Maps JavaScript API** (optional, for future map views)
3. Create an API key → restrict it to your Render server IP
4. Copy the key → save as `GOOGLE_MAPS_API_KEY`

---

## Step 4 — Render Setup (Backend API)

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your **AutoRoutingNew** GitHub repo
3. Configure:
   - **Name:** autorouting-api
   - **Region:** Pick closest to Texas (Oregon or Ohio)
   - **Branch:** main
   - **Root Directory:** (leave blank)
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Starter ($7/mo) — **do not use free tier**
4. Add **Environment Variables** (from `.env.example`):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
   - `SUPABASE_ANON_KEY`
   - `GOOGLE_MAPS_API_KEY`
   - `FRONTEND_URL` (add this after Step 5)
5. Click **Create Web Service**
6. Once deployed, copy your Render URL (e.g. `https://autorouting-api.onrender.com`)
7. In Render → **Settings** → copy the **Deploy Hook URL** → save to GitHub secrets as `RENDER_DEPLOY_HOOK_URL`

---

## Step 5 — Vercel Setup (Frontend)

1. Go to [vercel.com](https://vercel.com) → **Add New Project**
2. Import from **AutoRoutingNew** GitHub repo
3. **Framework Preset:** Next.js
4. **Root Directory:** `frontend`
5. Add **Environment Variables**:
   - `NEXT_PUBLIC_SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `NEXT_PUBLIC_API_URL` = your Render URL from Step 4
6. Deploy
7. Copy your Vercel URL → go back to Render → add as `FRONTEND_URL` env var

---

## Step 6 — Data Sync Setup (Your PC)

1. On your PC, open a terminal/command prompt
2. Navigate to the `sync/` folder
3. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env`:
   ```
   cp .env.example .env
   ```
5. Edit `.env`:
   - Set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (from Step 1)
   - Set `EXCEL_DIR` to the folder where your Excel files are saved
     - Example: `EXCEL_DIR=C:/Users/You/OneDrive/Dispatch/`
6. Test it once:
   ```
   python sync.py --once
   ```
7. If successful, set it to run automatically every 5 minutes:

**Windows (Task Scheduler):**
- Open Task Scheduler → Create Basic Task
- Name: "AutoRouting Sync"
- Trigger: Daily → repeat every 5 minutes
- Action: Start a program
  - Program: `python`
  - Arguments: `sync.py --once`
  - Start in: (path to your sync folder)

Or run the continuous version in a background window:
```
python sync.py
```
(It loops every 5 minutes until you close it)

---

## Step 7 — First Login

1. Go to your Vercel URL (e.g. `https://autorouting.vercel.app`)
2. Log in with your admin email and password
3. Run a sync first (or press force sync in dashboard)
4. Go to **Dispatch Board** → select today's date → click **Run Dispatch**

---

## Excel Files Required

Make sure these files exist in your `EXCEL_DIR`:

| File | Table |
|------|-------|
| `Yard_Locations.xlsx` | yard_locations |
| `terminal_locations.xlsx` | terminal_locations |
| `site_details.xlsx` | site_details |
| `Auto_Routing_Drivers_Schedule.xlsx` | driver_schedules |
| `Driver_terminal_cards.xlsx` | driver_terminal_cards |
| `load_details.xlsx` | load_details |

**Important:** Add the `is_diesel_wet` column (0 or 1) to `terminal_locations.xlsx` before syncing.

---

## Adding Future Features (O365/OneDrive Sync)

Once you have Office 365 permissions, the sync can be updated to pull files directly from OneDrive rather than your local PC. This will be a simple addition to `sync/sync.py`.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Sync says file not found | Check EXCEL_DIR path in .env (use forward slashes) |
| API returns 401 | Check SUPABASE_ANON_KEY in Vercel env vars |
| No drivers showing | Check shift_date format in Excel matches YYYY-MM-DD after sync |
| Google Maps not working | Verify Routes API is enabled in Google Cloud console |
| Render cold start slow | Upgrade plan or add a uptime monitor to ping /health every 10 min |

---

## Project File Structure

```
AutoRoutingNew/
├── backend/
│   ├── api/main.py          ← FastAPI endpoints
│   ├── engine/
│   │   ├── routing_engine.py ← Core dispatch algorithm
│   │   ├── geo.py            ← Distance & Google Maps
│   │   └── data_loader.py    ← Supabase → Python objects
│   ├── models/models.py      ← Typed data models
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── app/             ← Next.js pages
│   │   ├── components/      ← UI components + tabs
│   │   ├── hooks/useAuth.tsx ← Auth context
│   │   └── lib/             ← Supabase + API clients
│   └── package.json
├── sync/
│   ├── sync.py              ← Excel → Supabase sync
│   └── requirements.txt
├── database/
│   └── schema.sql           ← Run this in Supabase SQL editor
├── render.yaml              ← Render deployment config
└── .github/workflows/       ← Auto-deploy on git push
```
