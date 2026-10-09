-- Schema do banco de eventos de equipamento.
-- Cada linha representa UMA transicao de estado (ligou ou desligou), nao cada leitura.
-- O backend executa este mesmo schema na inicializacao (CREATE ... IF NOT EXISTS),
-- entao rodar este arquivo manualmente e opcional.

CREATE TABLE IF NOT EXISTS equipamento_eventos (
    id              SERIAL PRIMARY KEY,
    equipamento_id  VARCHAR(50)  NOT NULL,
    ligado          BOOLEAN      NOT NULL,
    timestamp       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

ALTER TABLE equipamento_eventos ADD COLUMN IF NOT EXISTS temperatura NUMERIC(6,2);

CREATE INDEX IF NOT EXISTS idx_equip_eventos_equip_time
    ON equipamento_eventos (equipamento_id, timestamp DESC);

-- Consultas uteis:

-- Ultimo estado conhecido de cada equipamento
-- SELECT DISTINCT ON (equipamento_id) equipamento_id, ligado, timestamp
-- FROM equipamento_eventos
-- ORDER BY equipamento_id, timestamp DESC;

-- Historico de um equipamento
-- SELECT * FROM equipamento_eventos
-- WHERE equipamento_id = 'impressora-1'
-- ORDER BY timestamp DESC;

-- Tempo total ligado por equipamento
-- WITH pares AS (
--     SELECT equipamento_id, ligado, timestamp,
--            LEAD(timestamp) OVER (PARTITION BY equipamento_id ORDER BY timestamp) AS proximo
--     FROM equipamento_eventos
-- )
-- SELECT equipamento_id, SUM(COALESCE(proximo, NOW()) - timestamp) AS tempo_ligado
-- FROM pares
-- WHERE ligado = TRUE
-- GROUP BY equipamento_id;
