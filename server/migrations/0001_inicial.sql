-- Horizon Escuderia — esquema inicial
-- Convenções: ids = UUID (TEXT); datas = 'AAAA-MM-DD'; instantes = ISO-8601 UTC;
-- valores monetários = centavos (INTEGER) para precisão exata; version = controle de concorrência.

CREATE TABLE settings (
  key TEXT NOT NULL, scope TEXT NOT NULL CHECK (scope IN ('global','user')),
  user_id TEXT NOT NULL DEFAULT '', value TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT,
  PRIMARY KEY (key, scope, user_id)
);

CREATE TABLE seasons (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, year INTEGER NOT NULL,
  start_date TEXT, end_date TEXT, notes TEXT,
  budget_planned_cents INTEGER NOT NULL DEFAULT 0 CHECK (budget_planned_cents >= 0),
  fundraising_goal_cents INTEGER NOT NULL DEFAULT 0 CHECK (fundraising_goal_cents >= 0),
  opening_balance_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);

CREATE TABLE areas (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '#64748b',
  sort INTEGER NOT NULL DEFAULT 0, description TEXT, logo_file_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);

CREATE TABLE users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE COLLATE NOCASE,
  password_hash TEXT,
  role TEXT NOT NULL CHECK (role IN ('admin','gestor','membro','juiz')),
  is_teacher INTEGER NOT NULL DEFAULT 0 CHECK (is_teacher IN (0,1)),
  area_id TEXT REFERENCES areas(id), position TEXT, phone TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  must_change_password INTEGER NOT NULL DEFAULT 0,
  last_login_at TEXT, failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  ip TEXT, user_agent TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE password_resets (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_by TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
);

CREATE TABLE wbs_elements (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  parent_id TEXT REFERENCES wbs_elements(id), sort INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, description TEXT, area_id TEXT REFERENCES areas(id),
  owner_id TEXT REFERENCES users(id), duration_days INTEGER CHECK (duration_days IS NULL OR duration_days >= 0),
  start_date TEXT, end_date TEXT, cost_estimate_cents INTEGER CHECK (cost_estimate_cents IS NULL OR cost_estimate_cents >= 0),
  acceptance_criteria TEXT, manual_progress INTEGER CHECK (manual_progress IS NULL OR manual_progress BETWEEN 0 AND 100),
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_wbs_parent ON wbs_elements(season_id, parent_id, sort);

CREATE TABLE activities (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  area_id TEXT REFERENCES areas(id), wbs_id TEXT REFERENCES wbs_elements(id),
  seq INTEGER, title TEXT NOT NULL, description TEXT, objective TEXT,
  category TEXT, priority TEXT, owner_id TEXT REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'nao_iniciada' CHECK (status IN
    ('nao_iniciada','em_andamento','em_revisao','aguardando_aprovacao','bloqueada','concluida','cancelada')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  planned_start TEXT, planned_end TEXT, baseline_start TEXT, baseline_end TEXT,
  actual_start TEXT, actual_end TEXT,
  is_milestone INTEGER NOT NULL DEFAULT 0 CHECK (is_milestone IN (0,1)),
  completion_criteria TEXT, notes TEXT, next_action TEXT, milestone_ref TEXT,
  alert_days INTEGER CHECK (alert_days IS NULL OR alert_days BETWEEN 0 AND 365),
  delay_justification TEXT, delay_impact TEXT, recovery_plan TEXT,
  recovery_owner_id TEXT REFERENCES users(id), forecast_end TEXT,
  approval_status TEXT NOT NULL DEFAULT 'rascunho' CHECK (approval_status IN
    ('rascunho','em_execucao','aguardando_aprovacao','aprovada','reprovada','reenviada')),
  content_version INTEGER NOT NULL DEFAULT 1, approved_version INTEGER,
  submitted_by TEXT REFERENCES users(id), submitted_at TEXT,
  raci_exception TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT, custom TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT REFERENCES users(id),
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT,
  CHECK (planned_start IS NULL OR planned_end IS NULL OR planned_start <= planned_end),
  CHECK (actual_start IS NULL OR actual_end IS NULL OR actual_start <= actual_end)
);
CREATE INDEX idx_act_season ON activities(season_id, area_id);
CREATE INDEX idx_act_owner ON activities(owner_id);
CREATE INDEX idx_act_wbs ON activities(wbs_id);

CREATE TABLE activity_participants (
  activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id),
  PRIMARY KEY (activity_id, user_id)
);

CREATE TABLE dependencies (
  id TEXT PRIMARY KEY,
  predecessor_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  successor_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  type TEXT NOT NULL DEFAULT 'FS' CHECK (type IN ('FS','SS','FF','SF')),
  lag_days INTEGER NOT NULL DEFAULT 0 CHECK (lag_days BETWEEN -365 AND 365),
  created_by TEXT, created_at TEXT NOT NULL,
  UNIQUE (predecessor_id, successor_id),
  CHECK (predecessor_id <> successor_id)
);
CREATE INDEX idx_dep_succ ON dependencies(successor_id);

CREATE TABLE delay_records (
  id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  registered_by TEXT REFERENCES users(id), registered_at TEXT NOT NULL,
  planned_end TEXT, justification TEXT NOT NULL CHECK (length(trim(justification)) > 0),
  impact TEXT, recovery_plan TEXT, recovery_owner_id TEXT REFERENCES users(id), forecast_end TEXT
);

CREATE TABLE raci (
  activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('R','A','C','I')),
  PRIMARY KEY (activity_id, user_id)
);
-- Governança: no máximo um aprovador final (A) por atividade, garantido pelo banco.
CREATE UNIQUE INDEX idx_raci_one_a ON raci(activity_id) WHERE role = 'A';

CREATE TABLE approvals (
  id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('submetida','reenviada','aprovada','reprovada','invalidada','tentativa_bloqueada')),
  user_id TEXT REFERENCES users(id), at TEXT NOT NULL, content_version INTEGER, comment TEXT, blocked_reason TEXT
);
CREATE INDEX idx_appr_act ON approvals(activity_id, at);

CREATE TABLE risks (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  code TEXT NOT NULL, title TEXT NOT NULL, description TEXT, category TEXT,
  cause TEXT, risk_event TEXT, consequence TEXT,
  probability INTEGER CHECK (probability IS NULL OR probability BETWEEN 1 AND 10),
  impact INTEGER CHECK (impact IS NULL OR impact BETWEEN 1 AND 10),
  original_probability TEXT, original_impact TEXT, original_level TEXT,
  owner_id TEXT REFERENCES users(id), owner_text TEXT,
  preventive_plan TEXT, contingency_plan TEXT, mitigation TEXT, action_deadline TEXT,
  warning_indicators TEXT,
  status TEXT NOT NULL DEFAULT 'aberto' CHECK (status IN ('aberto','em_tratamento','monitorado','ocorrido','encerrado')),
  identified_at TEXT, last_review_at TEXT,
  residual_probability INTEGER CHECK (residual_probability IS NULL OR residual_probability BETWEEN 1 AND 10),
  residual_impact INTEGER CHECK (residual_impact IS NULL OR residual_impact BETWEEN 1 AND 10),
  occurrence_result TEXT, notes TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT,
  UNIQUE (season_id, code)
);
CREATE TABLE risk_areas (
  risk_id TEXT NOT NULL REFERENCES risks(id) ON DELETE CASCADE,
  area_id TEXT NOT NULL REFERENCES areas(id), PRIMARY KEY (risk_id, area_id)
);
CREATE TABLE risk_links (
  risk_id TEXT NOT NULL REFERENCES risks(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('activity','wbs')),
  entity_id TEXT NOT NULL, PRIMARY KEY (risk_id, entity_type, entity_id)
);
CREATE TABLE risk_actions (
  id TEXT PRIMARY KEY, risk_id TEXT NOT NULL REFERENCES risks(id) ON DELETE CASCADE,
  description TEXT NOT NULL, owner_id TEXT REFERENCES users(id), due_date TEXT,
  status TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','em_andamento','concluida','cancelada')),
  done_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE risk_reviews (
  id TEXT PRIMARY KEY, risk_id TEXT NOT NULL REFERENCES risks(id) ON DELETE CASCADE,
  at TEXT NOT NULL, user_id TEXT REFERENCES users(id),
  probability INTEGER, impact INTEGER, residual_probability INTEGER, residual_impact INTEGER, note TEXT
);

CREATE TABLE events (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  name TEXT NOT NULL, type TEXT, description TEXT,
  start_date TEXT NOT NULL, end_date TEXT, start_time TEXT, location TEXT,
  owner_id TEXT REFERENCES users(id), area_id TEXT REFERENCES areas(id),
  objectives TEXT, checklist TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'ativo' CHECK (status IN ('ativo','cancelado','realizado')),
  source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT,
  CHECK (end_date IS NULL OR start_date <= end_date)
);

CREATE TABLE notifications (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
  entity_type TEXT, entity_id TEXT, dedupe_key TEXT NOT NULL,
  created_at TEXT NOT NULL, read_at TEXT, email_status TEXT,
  UNIQUE (user_id, dedupe_key)
);
CREATE INDEX idx_notif_user ON notifications(user_id, read_at, created_at);

CREATE TABLE evidence_weeks (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  month TEXT NOT NULL, week_number INTEGER NOT NULL CHECK (week_number BETWEEN 1 AND 6),
  start_date TEXT, end_date TEXT, supervisor_id TEXT REFERENCES users(id),
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT,
  UNIQUE (season_id, month, week_number),
  CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date)
);
CREATE TABLE evidence_entries (
  id TEXT PRIMARY KEY, week_id TEXT NOT NULL REFERENCES evidence_weeks(id) ON DELETE CASCADE,
  area_id TEXT NOT NULL REFERENCES areas(id), record_date TEXT,
  activities_done TEXT, deliverables TEXT, results TEXT, difficulties TEXT, decisions TEXT,
  next_actions TEXT, pending TEXT, extras TEXT, manager_comment TEXT,
  status TEXT NOT NULL DEFAULT 'rascunho' CHECK (status IN ('rascunho','registrado')),
  m_goals_met INTEGER CHECK (m_goals_met IS NULL OR m_goals_met >= 0),
  m_goals_total INTEGER CHECK (m_goals_total IS NULL OR m_goals_total >= 0),
  m_quality INTEGER CHECK (m_quality IS NULL OR m_quality BETWEEN 1 AND 5),
  source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (week_id, area_id)
);
CREATE TABLE evidence_members (
  entry_id TEXT NOT NULL REFERENCES evidence_entries(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY (entry_id, user_id)
);

CREATE TABLE evidence_activities (
  entry_id TEXT NOT NULL REFERENCES evidence_entries(id) ON DELETE CASCADE,
  activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE, PRIMARY KEY (entry_id, activity_id)
);

CREATE TABLE files (
  id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  data BLOB NOT NULL, judge_visible INTEGER NOT NULL DEFAULT 0,
  uploaded_by TEXT REFERENCES users(id), created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE INDEX idx_files_entity ON files(entity_type, entity_id);

CREATE TABLE comments (
  id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  author_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL CHECK (length(trim(body)) > 0),
  mentions TEXT, created_at TEXT NOT NULL, deleted_at TEXT, deleted_by TEXT
);
CREATE INDEX idx_comments_entity ON comments(entity_type, entity_id, created_at);

CREATE TABLE stakeholders (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  name TEXT NOT NULL, type TEXT, contact_name TEXT, email TEXT, phone TEXT,
  purpose TEXT, need TEXT, area_id TEXT REFERENCES areas(id), contribution_type TEXT,
  requested_cents INTEGER CHECK (requested_cents IS NULL OR requested_cents >= 0),
  confirmed_cents INTEGER CHECK (confirmed_cents IS NULL OR confirmed_cents >= 0),
  level TEXT, first_contact_date TEXT, proposal_date TEXT, confirmation_date TEXT,
  expected_receipt_date TEXT,
  status TEXT NOT NULL DEFAULT 'prospectado' CHECK (status IN
    ('prospectado','primeiro_contato','em_negociacao','proposta_enviada','aguardando_resposta',
     'confirmado','parcialmente_confirmado','recusado','concluido')),
  owner_id TEXT REFERENCES users(id), next_action TEXT, notes TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);
CREATE TABLE counterparts (
  id TEXT PRIMARY KEY, stakeholder_id TEXT NOT NULL REFERENCES stakeholders(id) ON DELETE CASCADE,
  kind TEXT, description TEXT NOT NULL, due_date TEXT,
  status TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','cumprida','cancelada')),
  done_date TEXT, record_note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  -- Uma contrapartida só é "cumprida" com registro correspondente (data + descrição do registro).
  CHECK (status <> 'cumprida' OR (done_date IS NOT NULL AND length(trim(coalesce(record_note,''))) > 0))
);

CREATE TABLE expenses (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  name TEXT NOT NULL, description TEXT, category TEXT, area_id TEXT REFERENCES areas(id),
  responsible_id TEXT REFERENCES users(id),
  request_date TEXT, purchase_date TEXT, due_date TEXT,
  supplier TEXT, funding_source TEXT, payment_method TEXT,
  quantity REAL CHECK (quantity IS NULL OR quantity >= 0), unit TEXT,
  unit_planned_cents INTEGER CHECK (unit_planned_cents IS NULL OR unit_planned_cents >= 0),
  unit_actual_cents INTEGER CHECK (unit_actual_cents IS NULL OR unit_actual_cents >= 0),
  total_planned_cents INTEGER CHECK (total_planned_cents IS NULL OR total_planned_cents >= 0),
  total_actual_cents INTEGER CHECK (total_actual_cents IS NULL OR total_actual_cents >= 0),
  acquisition_type TEXT NOT NULL DEFAULT 'proprio',
  is_cash INTEGER NOT NULL DEFAULT 1 CHECK (is_cash IN (0,1)),
  stakeholder_id TEXT REFERENCES stakeholders(id),
  status TEXT NOT NULL DEFAULT 'planejada' CHECK (status IN ('planejada','solicitada','comprometida','realizada','cancelada')),
  notes TEXT, needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);
CREATE TABLE incomes (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  description TEXT NOT NULL, purpose TEXT, source TEXT, stakeholder_id TEXT REFERENCES stakeholders(id),
  area_id TEXT REFERENCES areas(id), expected_date TEXT,
  expected_cents INTEGER NOT NULL DEFAULT 0 CHECK (expected_cents >= 0),
  method TEXT,
  status TEXT NOT NULL DEFAULT 'prevista' CHECK (status IN ('prevista','confirmada','cancelada')),
  notes TEXT, needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, import_source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);
-- Movimentos de caixa: única fonte do que foi efetivamente pago/recebido. Imutáveis;
-- correções são feitas por estorno (novo movimento com valor negativo apontando para o original).
CREATE TABLE movements (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  date TEXT NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('entrada','saida')),
  amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0),
  origin_type TEXT NOT NULL CHECK (origin_type IN ('expense','income')),
  origin_id TEXT NOT NULL, method TEXT, note TEXT,
  reverses_id TEXT REFERENCES movements(id),
  created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE INDEX idx_mov_origin ON movements(origin_type, origin_id);
CREATE INDEX idx_mov_season ON movements(season_id, date);
CREATE UNIQUE INDEX idx_mov_one_reversal ON movements(reverses_id) WHERE reverses_id IS NOT NULL;

CREATE TABLE scopes (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL UNIQUE REFERENCES seasons(id),
  data TEXT NOT NULL, doc_version INTEGER NOT NULL DEFAULT 1,
  needs_review INTEGER NOT NULL DEFAULT 0, review_note TEXT, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT, updated_by TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE scope_versions (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
  doc_version INTEGER NOT NULL, data TEXT NOT NULL, note TEXT,
  created_by TEXT, created_at TEXT NOT NULL
);
CREATE TABLE change_requests (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  title TEXT NOT NULL, description TEXT, justification TEXT NOT NULL,
  impact_schedule TEXT, impact_budget TEXT, impact_other TEXT,
  status TEXT NOT NULL DEFAULT 'aberta' CHECK (status IN ('aberta','aprovada','rejeitada','implementada')),
  requested_by TEXT REFERENCES users(id), decided_by TEXT REFERENCES users(id), decided_at TEXT, decision_note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
  version INTEGER NOT NULL DEFAULT 1, archived_at TEXT, deleted_at TEXT
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
  user_id TEXT, user_name TEXT, action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_id TEXT, summary TEXT,
  before TEXT, after TEXT, ip TEXT
);
CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id, id);
CREATE INDEX idx_audit_at ON audit_log(at);

CREATE TABLE imports (
  id TEXT PRIMARY KEY, created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'em_revisao' CHECK (status IN ('em_revisao','confirmada','descartada')),
  season_id TEXT REFERENCES seasons(id), files TEXT NOT NULL, staging TEXT NOT NULL,
  summary TEXT, confirmed_at TEXT
);

CREATE TABLE templates (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL,
  created_by TEXT, created_at TEXT NOT NULL, deleted_at TEXT
);

CREATE TABLE idempotency (
  key TEXT NOT NULL, user_id TEXT NOT NULL, path TEXT NOT NULL,
  status INTEGER NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (key, user_id)
);

CREATE TABLE job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, ok INTEGER, result TEXT
);
