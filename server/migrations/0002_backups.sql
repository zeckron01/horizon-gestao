-- Backups lógicos guardados no próprio banco (usado quando não há disco persistente, ex.: PostgreSQL/Neon na Vercel).
-- No modo SQLite os backups continuam sendo arquivos na pasta de backups e esta tabela fica vazia.
CREATE TABLE backups (
  name TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  size INTEGER NOT NULL,
  data BLOB NOT NULL
);
