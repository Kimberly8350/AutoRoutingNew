-- ============================================================
-- AUTO ROUTING - SUPABASE DATABASE SCHEMA
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- AUTH / USERS
-- ============================================================
CREATE TABLE IF NOT EXISTS app_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'administrator')),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- YARDS
-- ============================================================
CREATE TABLE IF NOT EXISTS yard_locations (
    yard TEXT PRIMARY KEY,
    longitude DOUBLE PRECISION,
    latitude DOUBLE PRECISION,
    zip TEXT,
    state TEXT,
    city TEXT,
    yard_address TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TERMINALS
-- ============================================================
CREATE TABLE IF NOT EXISTS terminal_locations (
    terminal_id TEXT PRIMARY KEY,  -- ODBC string code, e.g. "T-01-TX-0001"
    terminal_abbreviation TEXT,
    terminal_name TEXT NOT NULL,
    terminal_address TEXT,
    city TEXT,
    state TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    is_diesel_wet INTEGER DEFAULT 0 CHECK (is_diesel_wet IN (0, 1)),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- SITES
-- ============================================================
CREATE TABLE IF NOT EXISTS site_details (
    site_id INTEGER PRIMARY KEY,
    customer_group_name TEXT,
    site_name TEXT NOT NULL,
    site_address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    longitude DOUBLE PRECISION,
    latitude DOUBLE PRECISION,
    pump_certified INTEGER DEFAULT 0 CHECK (pump_certified IN (0, 1)),
    branded INTEGER,
    brand TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- DRIVER SCHEDULE (master schedule table)
-- ============================================================
CREATE TABLE IF NOT EXISTS driver_schedules (
    record_id INTEGER PRIMARY KEY,
    driver_id INTEGER NOT NULL,
    first_name TEXT,
    last_name TEXT,
    driver_start_time TIME,
    division_prefix TEXT,
    default_shift_name TEXT,
    board_location TEXT CHECK (board_location IN ('TX-AM','TX-PM','FW-AM','FW-PM','ET-AM')),
    yard TEXT,
    shift_date DATE,
    driver_schedule INTEGER DEFAULT 0 CHECK (driver_schedule IN (0,1)),
    attendance_expected INTEGER DEFAULT 0 CHECK (attendance_expected IN (0,1)),
    pump_trained INTEGER DEFAULT 0 CHECK (pump_trained IN (0,1)),
    max_shift_hours NUMERIC(4,2) DEFAULT 12.0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_driver_schedules_date ON driver_schedules(shift_date);
CREATE INDEX IF NOT EXISTS idx_driver_schedules_driver ON driver_schedules(driver_id);

-- ============================================================
-- DRIVER TERMINAL CARDS (access)
-- ============================================================
CREATE TABLE IF NOT EXISTS driver_terminal_cards (
    id SERIAL PRIMARY KEY,
    driver_id INTEGER NOT NULL,
    last_name TEXT,
    first_name TEXT,
    terminal_name TEXT,
    terminal_id TEXT,  -- ODBC string code, e.g. "T-01-TX-0001"
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(driver_id, terminal_id)
);

CREATE INDEX IF NOT EXISTS idx_dtc_driver ON driver_terminal_cards(driver_id);
CREATE INDEX IF NOT EXISTS idx_dtc_terminal ON driver_terminal_cards(terminal_id);

-- ============================================================
-- DRIVER RESTRICTIONS (sites and customers)
-- ============================================================
CREATE TABLE IF NOT EXISTS driver_restrictions (
    id SERIAL PRIMARY KEY,
    driver_id INTEGER NOT NULL,
    restriction_type TEXT NOT NULL CHECK (restriction_type IN ('site', 'customer')),
    site_id INTEGER,
    customer_group_name TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by UUID REFERENCES app_users(id)
);

CREATE INDEX IF NOT EXISTS idx_restrictions_driver ON driver_restrictions(driver_id);

-- ============================================================
-- LOADS
-- ============================================================
CREATE TABLE IF NOT EXISTS load_details (
    ce_id INTEGER NOT NULL,
    delivery_date DATE,
    customer_name TEXT,
    order_number TEXT,
    site_id INTEGER,
    terminal_id TEXT,  -- ODBC string code, e.g. "T-01-TX-0001"
    terminal_name TEXT,
    product_name TEXT,
    gross_gallons NUMERIC(10,2),
    load_status_description TEXT,
    city TEXT,
    state TEXT,
    site_name TEXT,
    site_address TEXT,
    first_name TEXT,
    last_name TEXT,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    delivery_eta TIMESTAMPTZ,
    load_status INTEGER,
    arrived_at_rack TIMESTAMPTZ,
    left_rack TIMESTAMPTZ,
    arrived_at_site TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ce_id, product_name)
);

CREATE INDEX IF NOT EXISTS idx_loads_date ON load_details(delivery_date);
CREATE INDEX IF NOT EXISTS idx_loads_status ON load_details(load_status);
CREATE INDEX IF NOT EXISTS idx_loads_ce ON load_details(ce_id);

-- ============================================================
-- DISPATCH RESULTS (routing engine output)
-- ============================================================
CREATE TABLE IF NOT EXISTS dispatch_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dispatch_date DATE NOT NULL,
    board_location TEXT,
    driver_id INTEGER NOT NULL,
    driver_name TEXT,
    route_sequence INTEGER NOT NULL,
    ce_id INTEGER NOT NULL,
    site_name TEXT,
    site_city TEXT,
    customer_name TEXT,
    terminal_name TEXT,
    terminal_id TEXT,  -- ODBC string code, e.g. "T-01-TX-0001"
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    eta TIMESTAMPTZ,
    drive_to_terminal_mins NUMERIC(8,2),
    drive_to_site_mins NUMERIC(8,2),
    total_loaded_miles NUMERIC(10,2),
    total_empty_miles NUMERIC(10,2),
    status TEXT DEFAULT 'assigned',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by UUID REFERENCES app_users(id),
    run_id UUID
);

CREATE INDEX IF NOT EXISTS idx_dispatch_date ON dispatch_results(dispatch_date);
CREATE INDEX IF NOT EXISTS idx_dispatch_driver ON dispatch_results(driver_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_run ON dispatch_results(run_id);

-- ============================================================
-- UNASSIGNED LOADS (routing engine output)
-- ============================================================
CREATE TABLE IF NOT EXISTS unassigned_loads (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dispatch_date DATE NOT NULL,
    ce_id INTEGER NOT NULL,
    site_name TEXT,
    reason TEXT,
    reason_category TEXT,
    run_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_unassigned_date ON unassigned_loads(dispatch_date);
CREATE INDEX IF NOT EXISTS idx_unassigned_run ON unassigned_loads(run_id);

-- ============================================================
-- DISPATCH RUNS (audit log of each routing run)
-- ============================================================
CREATE TABLE IF NOT EXISTS dispatch_runs (
    run_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dispatch_date DATE NOT NULL,
    run_type TEXT DEFAULT 'dispatch' CHECK (run_type IN ('dispatch','reroute')),
    total_loads INTEGER,
    assigned_loads INTEGER,
    unassigned_loads INTEGER,
    run_duration_ms INTEGER,
    run_by UUID REFERENCES app_users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE  -- only one active run per date
);

-- ============================================================
-- SYNC LOG (track data sync from Excel)
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_log (
    id SERIAL PRIMARY KEY,
    synced_at TIMESTAMPTZ DEFAULT NOW(),
    table_name TEXT,
    rows_upserted INTEGER,
    rows_deleted INTEGER,
    status TEXT,
    error_message TEXT,
    duration_ms INTEGER
);

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE dispatch_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE unassigned_loads ENABLE ROW LEVEL SECURITY;
ALTER TABLE dispatch_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_restrictions ENABLE ROW LEVEL SECURITY;

-- All authenticated users can read dispatch data
DROP POLICY IF EXISTS "Authenticated users can read dispatch_results" ON dispatch_results;
CREATE POLICY "Authenticated users can read dispatch_results"
    ON dispatch_results FOR SELECT
    USING (auth.role() = 'authenticated');

DROP POLICY IF EXISTS "Authenticated users can read unassigned_loads" ON unassigned_loads;
CREATE POLICY "Authenticated users can read unassigned_loads"
    ON unassigned_loads FOR SELECT
    USING (auth.role() = 'authenticated');

-- Only admins can manage users
DROP POLICY IF EXISTS "Admins manage users" ON app_users;
CREATE POLICY "Admins manage users"
    ON app_users FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM app_users u
            WHERE u.id = auth.uid() AND u.role = 'administrator'
        )
    );

-- Triggers for updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_yard_updated ON yard_locations;
CREATE TRIGGER trg_yard_updated BEFORE UPDATE ON yard_locations FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_terminal_updated ON terminal_locations;
CREATE TRIGGER trg_terminal_updated BEFORE UPDATE ON terminal_locations FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_site_updated ON site_details;
CREATE TRIGGER trg_site_updated BEFORE UPDATE ON site_details FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_schedule_updated ON driver_schedules;
CREATE TRIGGER trg_schedule_updated BEFORE UPDATE ON driver_schedules FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_dtc_updated ON driver_terminal_cards;
CREATE TRIGGER trg_dtc_updated BEFORE UPDATE ON driver_terminal_cards FOR EACH ROW EXECUTE FUNCTION update_updated_at();
DROP TRIGGER IF EXISTS trg_load_updated ON load_details;
CREATE TRIGGER trg_load_updated BEFORE UPDATE ON load_details FOR EACH ROW EXECUTE FUNCTION update_updated_at();
