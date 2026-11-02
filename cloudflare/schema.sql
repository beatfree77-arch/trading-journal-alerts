-- =====================================================
-- FOREX FACTORY JOURNAL
-- Cloudflare D1 database schema
-- =====================================================

-- 1. ECONOMIC EVENTS
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT,
    currency TEXT NOT NULL
        CHECK (currency IN (
            'USD', 'EUR', 'GBP', 'JPY',
            'AUD', 'NZD', 'CAD', 'CHF'
        )),
    name TEXT NOT NULL,
    impact TEXT NOT NULL
        CHECK (impact IN (
            'high', 'medium', 'low', 'holiday'
        )),
    forecast TEXT NOT NULL DEFAULT '',
    previous TEXT NOT NULL DEFAULT '',
    event_time TEXT NOT NULL,
    time_mode TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_time
ON events(event_time);

CREATE INDEX IF NOT EXISTS idx_events_currency_time
ON events(currency, event_time);


-- 2. SENT ALERTS
CREATE TABLE IF NOT EXISTS sent_alerts (
    alert_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    lead_minutes INTEGER NOT NULL
        CHECK (lead_minutes IN (15, 5)),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'sending', 'sent',
            'failed', 'unknown'
        )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_until TEXT,
    sent_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY (event_id)
        REFERENCES events(event_id),

    UNIQUE(event_id, lead_minutes)
);

CREATE INDEX IF NOT EXISTS idx_alerts_status
ON sent_alerts(status, next_attempt_at);


-- 3. DELIVERY ATTEMPTS
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    outcome TEXT NOT NULL
        CHECK (outcome IN (
            'accepted', 'rejected',
            'timeout', 'unknown'
        )),
    attempted_at TEXT NOT NULL,
    http_status INTEGER,
    error_message TEXT,

    FOREIGN KEY (alert_key)
        REFERENCES sent_alerts(alert_key)
);

CREATE INDEX IF NOT EXISTS idx_attempts_alert
ON delivery_attempts(alert_key, attempted_at);
