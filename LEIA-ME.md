# Horizon Gestão — pacote de publicação (Vercel + Neon)

Este repositório contém só o necessário para a Vercel executar a plataforma. O código-fonte completo, os testes e a
documentação estão no pacote `horizon-gestao-1.1.0.zip`; o passo a passo de publicação está em `docs/VERCEL-NEON.md`.

Variáveis de ambiente exigidas na Vercel: `DATABASE_URL` (PostgreSQL do Neon, conexão direta), `HORIZON_SETUP_TOKEN`
e `CRON_SECRET`. Opcionais: `HORIZON_PUBLIC_URL`, `SMTP_*`.

Não coloque senhas, o endereço do banco nem planilhas da equipe neste repositório.
