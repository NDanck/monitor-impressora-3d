# Monitor de estado de impressora 3D (IoT + Nuvem)

Detecta se a impressora está ligada ou desligada pela temperatura, grava apenas
as transições de estado, avisa no Discord e exibe tudo em um dashboard web.

## Arquitetura

```
ESP32 / simulador (local)
   |  MQTT/TLS 8883  equipamentos/<id>/temperatura
   v
HiveMQ Cloud (SaaS) <------------------------------------------+
   |                                                           |
   v                                                           |
Backend Python - FastAPI + paho-mqtt (Render, PaaS)            |
   |-- grava transições -----------> PostgreSQL (Neon, PaaS)   |
   |-- publica equipamentos/<id>/status ----> Bot Discord (local)
   |-- publica equipamentos/<id>/comando ---> ESP32 (LED, intervalo)
   |
   +-- API REST <---- Dashboard HTML/CSS/JS (Vercel, PaaS)
```

| Pasta | Conteúdo |
|---|---|
| `backend/` | API REST + assinante MQTT + regra de histerese |
| `frontend/` | Dashboard estático (Vercel) |
| `bot/` | Bot Discord, assina `equipamentos/+/status` |
| `sensor/` | Simulador do ESP32 com processamento na borda |
| `db/init.sql` | Schema (o backend também cria o schema ao iniciar) |
| `docker-compose.yml` | Alternativa: backend + PostgreSQL locais |

## Regra de estado (histerese)

| Temperatura | Estado anterior | Ação |
|---|---|---|
| > `TEMP_LIGA` (40 °C) | diferente de ligado | grava LIGADO, avisa Discord, envia `led_on` |
| < `TEMP_DESLIGA` (35 °C) | diferente de desligado | grava DESLIGADO, avisa Discord, envia `led_off` |
| entre 35 e 40 °C | qualquer | nada (evita oscilação) |
| demais casos | — | nada |

Uma linha no banco por transição, nunca por leitura.

## Tópicos MQTT

| Tópico | Direção | Payload |
|---|---|---|
| `equipamentos/<id>/temperatura` | ESP32 → nuvem | `{"temperatura": 41.2}` |
| `equipamentos/<id>/status` | backend → bot | `{"equipamento_id", "status", "temperatura", "timestamp"}` |
| `equipamentos/<id>/comando` | nuvem → ESP32 | `{"acao": "led_on"}`, `{"acao": "set_interval", "valor": 10}` |

Ações aceitas: `led_on`, `led_off`, `read_now`, `set_interval`.

## API

| Método | Rota | Retorno |
|---|---|---|
| GET | `/api/health` | status e conexão MQTT |
| GET | `/api/config` | limiares e intervalo esperado |
| GET | `/api/estado` | estado atual, última temperatura, online/offline |
| GET | `/api/historico?equipamento_id=&limite=` | transições, mais recente primeiro |
| GET | `/api/timeline?equipamento_id=&horas=` | segmentos ligado/desligado |
| GET | `/api/tempo-diario?equipamento_id=&dias=` | segundos ligada/desligada por dia |
| POST | `/api/equipamentos/{id}/comando` | publica comando para o dispositivo |

Documentação interativa: `<URL do backend>/docs`.

## Implantação

### 1. HiveMQ Cloud

1. Criar conta em console.hivemq.cloud e um cluster **Serverless** (gratuito).
2. **Access Management**: criar credenciais (ex.: `backend`, `bot`, `sensor`, ou uma única para a demo).
3. Copiar o host do cluster (`xxxxxxxx.s1.eu.hivemq.cloud`). Porta 8883 (TLS).

### 2. PostgreSQL (Neon)

1. Criar projeto em neon.tech.
2. Copiar a connection string (`postgresql://...?sslmode=require`).
3. Nada mais: o backend cria a tabela ao iniciar.

Alternativa: Render Postgres (o plano gratuito expira em 30 dias).

### 3. Backend (Render)

O Render implanta a partir de um repositório Git (GitHub/GitLab).

1. Subir este projeto para um repositório.
2. Render → **New → Web Service** → selecionar o repositório.
3. **Root Directory**: `backend` | **Runtime**: Docker | **Instance**: Free.
4. **Environment**: copiar as variáveis de `backend/.env.example` com os valores reais
   (`MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`, `DATABASE_URL`, etc.).
5. **Health Check Path**: `/api/health`.
6. Após o deploy, testar: `https://<servico>.onrender.com/api/health`.

O plano gratuito suspende o serviço após 15 min sem requisições HTTP e leva cerca de
1 min para voltar. Com o serviço suspenso, o backend não recebe MQTT. O dashboard aberto
mantém o serviço ativo (consulta a API a cada 5 s). Abrir o dashboard alguns minutos
antes da apresentação.

### 4. Dashboard (Vercel)

1. Editar `frontend/config.js`: `window.API_URL = "https://<servico>.onrender.com";`
2. Vercel → **Add New → Project** → repositório → **Root Directory**: `frontend`
   → **Framework Preset**: Other → sem build command. Deploy.
   (Alternativa sem Git: `cd frontend && npx vercel`.)
3. No Render, definir `CORS_ORIGINS=https://<projeto>.vercel.app` (ou manter `*`).

A URL da API também pode ser trocada sem novo deploy: `https://<projeto>.vercel.app/?api=https://...`

### 5. Bot Discord (local)

```bash
cd bot
cp .env.example .env      # DISCORD_TOKEN, DISCORD_CHANNEL_ID, credenciais MQTT
pip install -r requirements.txt
python main.py
```

### 6. Simulador do sensor (local)

```bash
cd sensor
cp .env.example .env      # credenciais MQTT
pip install -r requirements.txt
python sensor_simulator.py
```

O simulador aquece até 60 °C, mantém, resfria até 25 °C e repete. Com os valores
padrão (`INTERVALO_S=10`, `PASSO_C=4`, `CICLOS_PATAMAR=6`) cada ciclo completo leva
cerca de 4 min. Processamento na borda: 5 amostras por ciclo, descarte de leituras
fora de -10 a 125 °C (falha simulada de 5%) e envio apenas da média.

## Execução totalmente local (opcional)

```bash
cp backend/.env.example backend/.env   # preencher credenciais do HiveMQ Cloud
docker compose up -d --build           # backend em http://localhost:8000 + PostgreSQL
```

Abrir `frontend/index.html` com `?api=http://localhost:8000`, ou servir a pasta:
`cd frontend && python -m http.server 5500`.

## Valores de demonstração vs. produção

| Variável | Demo | Produção |
|---|---|---|
| `INTERVALO_S` (sensor) | 10 | 600 |
| `INTERVALO_ESPERADO_S` (backend) | 10 | 600 |

O intervalo do sensor também pode ser alterado em tempo real pelo dashboard
(**Aplicar intervalo**), o que demonstra o fluxo nuvem → dispositivo.

## ESP32 real

Mesmos tópicos e payloads do simulador. Para TLS no ESP32, usar `WiFiClientSecure`;
na demo, `setInsecure()` funciona (tráfego criptografado, sem validar certificado).
O LED embutido normalmente fica no GPIO 2.
