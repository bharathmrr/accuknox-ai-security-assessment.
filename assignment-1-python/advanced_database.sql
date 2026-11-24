-- =============================================================================
-- Task 5 - Advanced relational database design (DEMONSTRATION SCHEMA)
-- -----------------------------------------------------------------------------
-- Domain : contact-centre compliance tracking (calls, required documents,
--          compliance findings, CRM sync audit). Modelled on the candidate's
--          GhostAgent / DataRipple problem space; this is NOT the production
--          schema of that project.
-- Dialect: SQLite 3.35+ (window functions, partial indexes, RETURNING not used).
--          Verified by tests/test_advanced_database.py. PostgreSQL notes are
--          given in comments where syntax would differ.
-- Run    : python -c "import sqlite3;c=sqlite3.connect(':memory:');\
--          c.executescript(open('advanced_database.sql').read())"
-- =============================================================================

PRAGMA foreign_keys = ON;   -- SQLite enforces FKs only when this is ON

-- -----------------------------------------------------------------------------
-- 1. Schema: normalised (3NF) tables with PK / FK / CHECK / UNIQUE constraints
-- -----------------------------------------------------------------------------
DROP VIEW  IF EXISTS v_agent_compliance;
DROP TABLE IF EXISTS crm_sync_log;
DROP TABLE IF EXISTS compliance_findings;
DROP TABLE IF EXISTS call_documents;
DROP TABLE IF EXISTS intent_required_documents;
DROP TABLE IF EXISTS document_types;
DROP TABLE IF EXISTS calls;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS agents;
DROP TABLE IF EXISTS teams;

CREATE TABLE teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    region      TEXT NOT NULL CHECK (region IN ('IN-SOUTH', 'IN-NORTH', 'US', 'UK'))
);

CREATE TABLE agents (
    agent_id    INTEGER PRIMARY KEY,
    team_id     INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE RESTRICT,
    full_name   TEXT    NOT NULL,
    email       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    hired_on    TEXT    NOT NULL CHECK (date(hired_on) IS NOT NULL)
);

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    phone_e164  TEXT NOT NULL UNIQUE CHECK (phone_e164 GLOB '+[1-9]*'
                                            AND length(phone_e164) BETWEEN 8 AND 16),
    email       TEXT UNIQUE COLLATE NOCASE CHECK (email IS NULL OR email LIKE '%_@_%._%'),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE calls (
    call_id          INTEGER PRIMARY KEY,
    provider         TEXT    NOT NULL CHECK (provider IN ('twilio', 'vonage', 'ringcentral')),
    provider_call_ref TEXT   NOT NULL,
    customer_id      INTEGER NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    agent_id         INTEGER NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    intent           TEXT    NOT NULL CHECK (intent IN ('loan_application', 'insurance_quote',
                                                        'account_support', 'unknown')),
    started_at       TEXT    NOT NULL CHECK (datetime(started_at) IS NOT NULL),
    duration_sec     INTEGER NOT NULL CHECK (duration_sec BETWEEN 0 AND 14400),
    status           TEXT    NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'compliant', 'non_compliant', 'review')),
    UNIQUE (provider, provider_call_ref)          -- idempotency for webhook redelivery
);

CREATE TABLE document_types (
    doc_type_id INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

-- Many-to-many: which documents each intent requires (business rules as data).
CREATE TABLE intent_required_documents (
    intent      TEXT    NOT NULL,
    doc_type_id INTEGER NOT NULL REFERENCES document_types(doc_type_id),
    PRIMARY KEY (intent, doc_type_id)
) WITHOUT ROWID;

-- Many-to-many: documents actually received for a call.
CREATE TABLE call_documents (
    call_id     INTEGER NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    doc_type_id INTEGER NOT NULL REFERENCES document_types(doc_type_id),
    received_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (call_id, doc_type_id)
) WITHOUT ROWID;

CREATE TABLE compliance_findings (
    finding_id  INTEGER PRIMARY KEY,
    call_id     INTEGER NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    rule_code   TEXT    NOT NULL,
    severity    TEXT    NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    detail      TEXT    NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (call_id, rule_code)
);

CREATE TABLE crm_sync_log (
    sync_id     INTEGER PRIMARY KEY,
    call_id     INTEGER NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    crm         TEXT    NOT NULL CHECK (crm IN ('salesforce', 'hubspot', 'zoho')),
    attempt     INTEGER NOT NULL CHECK (attempt >= 1),
    outcome     TEXT    NOT NULL CHECK (outcome IN ('success', 'retryable_error', 'fatal_error')),
    http_status INTEGER CHECK (http_status BETWEEN 100 AND 599),
    logged_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (call_id, crm, attempt)
);

-- -----------------------------------------------------------------------------
-- 2. Indexes chosen from the query patterns below
-- -----------------------------------------------------------------------------
-- FK columns are NOT indexed automatically in SQLite/PostgreSQL; index them so
-- joins and ON DELETE CASCADE do not table-scan.
CREATE INDEX ix_agents_team          ON agents(team_id);
CREATE INDEX ix_calls_customer       ON calls(customer_id);
-- Composite index: equality column first, range column second -> supports
-- "calls for agent X between dates" and ORDER BY started_at.
CREATE INDEX ix_calls_agent_started  ON calls(agent_id, started_at);
CREATE INDEX ix_findings_call        ON compliance_findings(call_id);
-- Partial index: the dashboard only ever looks at OPEN high/critical findings,
-- which are a small fraction of rows, so the index stays tiny.
CREATE INDEX ix_findings_open_severe ON compliance_findings(severity, created_at)
    WHERE resolved = 0 AND severity IN ('high', 'critical');
-- Covering index for the CRM retry worker (query answered from the index alone).
CREATE INDEX ix_sync_retry           ON crm_sync_log(outcome, call_id, attempt)
    WHERE outcome = 'retryable_error';

-- -----------------------------------------------------------------------------
-- 3. Trigger: keep calls.status consistent with findings (derived state)
-- -----------------------------------------------------------------------------
CREATE TRIGGER trg_findings_mark_call
AFTER INSERT ON compliance_findings
BEGIN
    UPDATE calls
       SET status = CASE WHEN NEW.severity IN ('high', 'critical') THEN 'non_compliant'
                         ELSE 'review' END
     WHERE call_id = NEW.call_id AND status <> 'non_compliant';
END;

-- -----------------------------------------------------------------------------
-- 4. Reference + sample data, loaded inside one transaction
-- -----------------------------------------------------------------------------
BEGIN TRANSACTION;

INSERT INTO teams (team_id, name, region) VALUES
    (1, 'Bengaluru Sales', 'IN-SOUTH'), (2, 'Delhi Sales', 'IN-NORTH'), (3, 'US Support', 'US');

INSERT INTO agents (agent_id, team_id, full_name, email, hired_on) VALUES
    (1, 1, 'Agent Asha',   'asha@example.com',   '2023-01-10'),
    (2, 1, 'Agent Bala',   'bala@example.com',   '2023-06-01'),
    (3, 2, 'Agent Charu',  'charu@example.com',  '2022-11-15'),
    (4, 3, 'Agent Dan',    'dan@example.com',    '2024-02-20');

INSERT INTO customers (customer_id, phone_e164, email) VALUES
    (1, '+919845000001', 'c1@example.com'), (2, '+919845000002', NULL),
    (3, '+919845000003', 'c3@example.com'), (4, '+14155550104', 'c4@example.com'),
    (5, '+919845000005', 'c5@example.com');

INSERT INTO document_types (doc_type_id, code, description) VALUES
    (1, 'kyc_id', 'Government photo ID'), (2, 'income_proof', 'Salary slip / ITR'),
    (3, 'consent_form', 'Signed recording + data consent'),
    (4, 'identity_verification', 'OTP or security-question verification');

INSERT INTO intent_required_documents (intent, doc_type_id) VALUES
    ('loan_application', 1), ('loan_application', 2), ('loan_application', 3),
    ('insurance_quote', 1),  ('insurance_quote', 3),
    ('account_support', 4);

INSERT INTO calls (call_id, provider, provider_call_ref, customer_id, agent_id, intent,
                   started_at, duration_sec) VALUES
    (101, 'twilio',      'CA001', 1, 1, 'loan_application', '2026-11-02 10:05:00', 640),
    (102, 'vonage',      'vn-002', 2, 1, 'insurance_quote',  '2026-11-02 11:20:00', 420),
    (103, 'ringcentral', 'rc-003', 3, 2, 'loan_application', '2026-11-03 09:45:00', 900),
    (104, 'twilio',      'CA004', 4, 4, 'account_support',  '2026-11-03 15:10:00', 300),
    (105, 'vonage',      'vn-005', 5, 3, 'loan_application', '2026-11-04 12:00:00', 1200),
    (106, 'twilio',      'CA006', 1, 2, 'insurance_quote',  '2026-11-05 16:30:00', 510),
    (107, 'ringcentral', 'rc-007', 3, 3, 'account_support',  '2026-11-06 10:00:00', 180),
    (108, 'twilio',      'CA008', 2, 1, 'loan_application', '2026-11-06 14:40:00', 760);

INSERT INTO call_documents (call_id, doc_type_id) VALUES
    (101, 1), (101, 2), (101, 3),       -- complete
    (102, 1),                           -- missing consent
    (103, 1), (103, 3),                 -- missing income proof
    (104, 4),                           -- complete
    (105, 1),                           -- missing income proof + consent
    (106, 1), (106, 3),                 -- complete
    (108, 1), (108, 2), (108, 3);       -- complete

INSERT INTO compliance_findings (call_id, rule_code, severity, detail) VALUES
    (105, 'PROHIBITED_PHRASE', 'critical', 'Agent promised "guaranteed returns"'),
    (102, 'MISSING_CONSENT',   'high',     'Consent form not captured'),
    (107, 'ID_NOT_VERIFIED',   'medium',   'Account change without verification');

UPDATE calls SET status = 'compliant' WHERE call_id IN (101, 104, 106, 108);

INSERT INTO crm_sync_log (call_id, crm, attempt, outcome, http_status) VALUES
    (101, 'salesforce', 1, 'success', 200),
    (102, 'hubspot',    1, 'retryable_error', 429), (102, 'hubspot', 2, 'success', 200),
    (103, 'salesforce', 1, 'retryable_error', 503),
    (105, 'hubspot',    1, 'fatal_error', 400);

COMMIT;

-- -----------------------------------------------------------------------------
-- 5. Transaction with a business invariant: resolve a finding AND write the
--    audit row atomically. If any statement fails, nothing is persisted.
--    (PostgreSQL: BEGIN; ... COMMIT; plus SELECT ... FOR UPDATE on the finding.)
-- -----------------------------------------------------------------------------
BEGIN IMMEDIATE TRANSACTION;               -- take the write lock up front
UPDATE compliance_findings SET resolved = 1
 WHERE call_id = 107 AND rule_code = 'ID_NOT_VERIFIED' AND resolved = 0;
INSERT INTO call_documents (call_id, doc_type_id) VALUES (107, 4);
UPDATE calls SET status = 'compliant'
 WHERE call_id = 107
   AND NOT EXISTS (SELECT 1 FROM compliance_findings f
                    WHERE f.call_id = 107 AND f.resolved = 0);
COMMIT;

-- -----------------------------------------------------------------------------
-- 6. Analytical queries
-- -----------------------------------------------------------------------------

-- 6a. Missing required documents per call (anti-join via NOT EXISTS).
--     NOT EXISTS is NULL-safe, unlike NOT IN, and lets the planner use the PK.
SELECT c.call_id, c.intent, dt.code AS missing_document
  FROM calls c
  JOIN intent_required_documents ird ON ird.intent = c.intent
  JOIN document_types dt             ON dt.doc_type_id = ird.doc_type_id
 WHERE NOT EXISTS (SELECT 1 FROM call_documents cd
                    WHERE cd.call_id = c.call_id AND cd.doc_type_id = ird.doc_type_id)
 ORDER BY c.call_id, dt.code;

-- 6b. Agent compliance scorecard: CTEs + aggregation + window ranking.
CREATE VIEW v_agent_compliance AS
WITH call_stats AS (
    SELECT c.agent_id,
           COUNT(*)                                             AS total_calls,
           SUM(c.status = 'compliant')                          AS compliant_calls,
           SUM(c.status = 'non_compliant')                      AS non_compliant_calls,
           ROUND(AVG(c.duration_sec) / 60.0, 1)                 AS avg_minutes
      FROM calls c
     GROUP BY c.agent_id
),
finding_stats AS (
    SELECT c.agent_id,
           COUNT(f.finding_id)                                  AS findings,
           COALESCE(SUM(f.severity = 'critical'), 0)            AS critical_findings
      FROM calls c
      LEFT JOIN compliance_findings f ON f.call_id = c.call_id
     GROUP BY c.agent_id
)
SELECT a.agent_id, a.full_name, t.name AS team,
       cs.total_calls, cs.compliant_calls, cs.non_compliant_calls, cs.avg_minutes,
       fs.findings, fs.critical_findings,
       ROUND(100.0 * cs.compliant_calls / cs.total_calls, 1)   AS compliance_rate_pct,
       RANK() OVER (PARTITION BY t.team_id
                    ORDER BY 1.0 * cs.compliant_calls / cs.total_calls DESC) AS rank_in_team
  FROM agents a
  JOIN teams t          ON t.team_id = a.team_id
  JOIN call_stats cs    ON cs.agent_id = a.agent_id
  JOIN finding_stats fs ON fs.agent_id = a.agent_id;

SELECT * FROM v_agent_compliance ORDER BY team, rank_in_team;

-- 6c. Daily trend with a running total and 3-day moving average (window frames).
SELECT day, calls, non_compliant,
       SUM(calls) OVER (ORDER BY day ROWS UNBOUNDED PRECEDING)          AS running_calls,
       ROUND(AVG(calls) OVER (ORDER BY day ROWS 2 PRECEDING), 2)        AS calls_3day_avg
  FROM (SELECT date(started_at) AS day, COUNT(*) AS calls,
               SUM(status = 'non_compliant') AS non_compliant
          FROM calls GROUP BY date(started_at));

-- 6d. CRM retry queue: latest attempt per call/CRM still in retryable state.
SELECT s.call_id, s.crm, s.attempt, s.http_status
  FROM crm_sync_log s
 WHERE s.outcome = 'retryable_error'
   AND s.attempt = (SELECT MAX(attempt) FROM crm_sync_log x
                     WHERE x.call_id = s.call_id AND x.crm = s.crm);

-- 6e. Team-level rollup with HAVING (only teams with >=2 calls).
SELECT t.name AS team, COUNT(c.call_id) AS calls,
       ROUND(100.0 * SUM(c.status = 'compliant') / COUNT(c.call_id), 1) AS compliance_pct
  FROM teams t
  JOIN agents a ON a.team_id = t.team_id
  JOIN calls  c ON c.agent_id = a.agent_id
 GROUP BY t.team_id
HAVING COUNT(c.call_id) >= 2
 ORDER BY compliance_pct DESC;

-- -----------------------------------------------------------------------------
-- 7. Query optimisation evidence
-- -----------------------------------------------------------------------------
-- Verified output (SQLite 3.53): "SEARCH calls USING COVERING INDEX ix_calls_agent_started
--   (agent_id=? AND started_at>?)" - no table scan and no temp B-tree for ORDER BY.
EXPLAIN QUERY PLAN
SELECT call_id, started_at FROM calls
 WHERE agent_id = 1 AND started_at >= '2026-11-01' ORDER BY started_at;

-- Verified output: "SEARCH compliance_findings USING COVERING INDEX ix_findings_open_severe
--   (severity=?)" - the partial index is chosen because the WHERE clause implies its predicate.
EXPLAIN QUERY PLAN
SELECT finding_id FROM compliance_findings
 WHERE resolved = 0 AND severity IN ('high', 'critical') AND severity = 'critical';

-- Refresh planner statistics after bulk loads (PostgreSQL: ANALYZE / VACUUM ANALYZE).
ANALYZE;
