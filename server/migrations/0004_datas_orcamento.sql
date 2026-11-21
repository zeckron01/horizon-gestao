-- Orçamento: quando o time precisa de cada material ou serviço e quando a entrega foi realizada.
-- Só acrescenta colunas: nenhum dado existente é alterado ou apagado.
ALTER TABLE expenses ADD COLUMN need_date TEXT;
ALTER TABLE expenses ADD COLUMN delivery_date TEXT;
