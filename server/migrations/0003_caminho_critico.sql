-- Caminho crítico do projeto (CPM), riscos ligados ao cronograma e cenários alternativos.
-- Só acrescenta colunas e tabelas: nenhum dado existente é alterado ou apagado.

-- Duração necessária informada pela equipe (dias de trabalho, conforme o calendário do projeto).
-- Vazia = a duração é calculada pelas datas planejadas.
ALTER TABLE activities ADD COLUMN duration_days INTEGER CHECK (duration_days IS NULL OR (duration_days >= 0 AND duration_days <= 3650));

-- Efeito estimado de um risco sobre uma atividade (dias adicionais caso o risco ocorra), usado nas simulações.
ALTER TABLE risk_links ADD COLUMN est_delay_days INTEGER CHECK (est_delay_days IS NULL OR (est_delay_days >= 0 AND est_delay_days <= 365));
ALTER TABLE risk_links ADD COLUMN note TEXT;

-- Configuração do cálculo por temporada (calendário, feriados, data-limite, limiares dos alertas).
CREATE TABLE cpm_config (
  season_id TEXT PRIMARY KEY REFERENCES seasons(id),
  data TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT REFERENCES users(id)
);

-- Cenários de simulação. Ficam separados do planejamento oficial; só alteram datas, atividades e dependências
-- oficiais quando um gestor/administrador aprova e aplica (o estado anterior fica guardado em applied_before).
CREATE TABLE cpm_scenarios (
  id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES seasons(id),
  name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'personalizado' CHECK (kind IN ('atraso_sem_mitigacao','mitigacao','alternativa','personalizado')),
  description TEXT, risk_id TEXT REFERENCES risks(id), base_scenario_id TEXT REFERENCES cpm_scenarios(id),
  changes TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'rascunho' CHECK (status IN ('rascunho','proposto','aprovado','rejeitado','arquivado')),
  decision_note TEXT, decided_by TEXT REFERENCES users(id), decided_at TEXT,
  applied_at TEXT, applied_before TEXT, applied_result TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT REFERENCES users(id),
  version INTEGER NOT NULL DEFAULT 1, deleted_at TEXT
);
CREATE INDEX idx_cpm_scen_season ON cpm_scenarios(season_id, status);
