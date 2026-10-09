"""
Backend IoT - monitoramento de estado de impressora 3D.

- Assina a telemetria de temperatura via MQTT (HiveMQ Cloud, TLS 8883).
- Aplica histerese: liga acima de TEMP_LIGA, desliga abaixo de TEMP_DESLIGA.
- Grava no PostgreSQL apenas as transicoes de estado.
- Publica o status via MQTT e avisa o Discord via webhook.
- Envia comandos para o dispositivo (Cloud -> Edge).
- Expoe API REST para o dashboard.

Executar com 1 worker apenas: o estado em memoria e a assinatura MQTT
nao podem ser duplicados.
"""
import os
import json
import time
import threading
import urllib.request
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg2
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

# ---------------------------------------------------------------- config
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TLS = os.getenv("MQTT_TLS", "true").lower() == "true"
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "backend-iot")

TOPIC_TEMPERATURA = "equipamentos/+/temperatura"
TOPIC_STATUS = "equipamentos/{equip_id}/status"
TOPIC_COMANDO = "equipamentos/{equip_id}/comando"

TEMP_LIGA = float(os.getenv("TEMP_LIGA", 40.0))
TEMP_DESLIGA = float(os.getenv("TEMP_DESLIGA", 35.0))

# Intervalo esperado entre leituras. Dispositivo sem dados por mais de
# 2 intervalos e considerado offline.
INTERVALO_ESPERADO_S = int(os.getenv("INTERVALO_ESPERADO_S", 600))

DATABASE_URL = os.getenv("DATABASE_URL")
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", 5432),
    "dbname": os.getenv("DB_NAME", "iot"),
    "user": os.getenv("DB_USER", "iot"),
    "password": os.getenv("DB_PASS", "iot"),
}

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]
TZ_LOCAL = ZoneInfo(os.getenv("TZ_LOCAL", "America/Sao_Paulo"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

ACOES_VALIDAS = {"led_on", "led_off", "read_now", "set_interval"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS equipamento_eventos (
    id              SERIAL PRIMARY KEY,
    equipamento_id  VARCHAR(50)  NOT NULL,
    ligado          BOOLEAN      NOT NULL,
    timestamp       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE equipamento_eventos ADD COLUMN IF NOT EXISTS temperatura NUMERIC(6,2);
CREATE INDEX IF NOT EXISTS idx_equip_eventos_equip_time
    ON equipamento_eventos (equipamento_id, timestamp DESC);
"""

# ---------------------------------------------------------------- estado
estado_atual: dict = {}      # equip_id -> ultimo estado gravado (bool)
ultima_leitura: dict = {}    # equip_id -> {"temperatura", "recebido_em"}
lock = threading.Lock()
mqtt_client: Optional[mqtt.Client] = None
mqtt_conectado = False


# ---------------------------------------------------------------- banco
def get_conn():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(**DB_CONFIG)


def consultar(sql: str, params: tuple = ()):
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def inicializar_banco(tentativas: int = 10):
    for i in range(1, tentativas + 1):
        try:
            with closing(get_conn()) as conn, conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                conn.commit()
            print("[DB] Schema verificado")
            return
        except psycopg2.OperationalError as e:
            print(f"[DB] Banco indisponivel ({i}/{tentativas}): {e}")
            time.sleep(3)
    raise RuntimeError("Banco de dados inacessivel")


def carregar_estado_inicial():
    rows = consultar(
        """
        SELECT DISTINCT ON (equipamento_id) equipamento_id, ligado
        FROM equipamento_eventos
        ORDER BY equipamento_id, timestamp DESC
        """
    )
    with lock:
        for equip_id, ligado in rows:
            estado_atual[equip_id] = ligado
    print(f"[INIT] Estado carregado: {estado_atual}")


def salvar_evento(equip_id: str, ligado: bool, temperatura: float) -> datetime:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO equipamento_eventos (equipamento_id, ligado, temperatura)
            VALUES (%s, %s, %s)
            RETURNING timestamp
            """,
            (equip_id, ligado, temperatura),
        )
        ts = cur.fetchone()[0]
        conn.commit()
        return ts


# ---------------------------------------------------------------- mqtt
def publicar(topic: str, payload: dict):
    if mqtt_client is None:
        raise RuntimeError("Cliente MQTT nao inicializado")
    mqtt_client.publish(topic, json.dumps(payload), qos=1)
    print(f"[PUB] {topic} -> {payload}")


def notificar_discord(equip_id: str, ligado: bool, temperatura: float, ts: datetime):
    """Envia o aviso de transicao ao canal do Discord via webhook (thread separada)."""
    if not DISCORD_WEBHOOK_URL:
        return
    data_hora = ts.astimezone(TZ_LOCAL).strftime("%d/%m/%Y %H:%M:%S")
    texto = (
        f"{data_hora} - {equip_id.upper()} "
        f"{'LIGADO' if ligado else 'DESLIGADO'} ({temperatura:.1f} °C)"
    )

    def enviar():
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=json.dumps({"content": texto}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "monitor-impressoras"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"[DISCORD] {texto}")
        except Exception as e:
            print(f"[DISCORD] Falha: {e}")

    threading.Thread(target=enviar, daemon=True).start()


def processar_leitura(equip_id: str, temperatura: float):
    """
    Histerese:
    - temperatura > TEMP_LIGA     e estado != ligado    -> grava LIGADO
    - temperatura < TEMP_DESLIGA  e estado != desligado -> grava DESLIGADO
    - demais casos                                      -> nada
    Entre TEMP_DESLIGA e TEMP_LIGA o estado e mantido (evita oscilacao).
    """
    with lock:
        ultima_leitura[equip_id] = {
            "temperatura": temperatura,
            "recebido_em": datetime.now(timezone.utc),
        }
        anterior = estado_atual.get(equip_id)

        novo = None
        if temperatura > TEMP_LIGA and anterior is not True:
            novo = True
        elif temperatura < TEMP_DESLIGA and anterior is not False:
            novo = False

        if novo is None:
            return

        ts = salvar_evento(equip_id, novo, temperatura)
        estado_atual[equip_id] = novo

    publicar(
        TOPIC_STATUS.format(equip_id=equip_id),
        {
            "equipamento_id": equip_id,
            "status": "ligado" if novo else "desligado",
            "temperatura": temperatura,
            "timestamp": ts.isoformat(),
        },
    )
    notificar_discord(equip_id, novo, temperatura, ts)
    # Cloud -> Edge: LED do dispositivo reflete o estado calculado na nuvem
    publicar(
        TOPIC_COMANDO.format(equip_id=equip_id),
        {"acao": "led_on" if novo else "led_off"},
    )


def on_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_conectado
    mqtt_conectado = not reason_code.is_failure
    print(f"[MQTT] Conexao: {reason_code}")
    if mqtt_conectado:
        client.subscribe(TOPIC_TEMPERATURA, qos=1)


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    global mqtt_conectado
    mqtt_conectado = False
    print(f"[MQTT] Desconectado: {reason_code}")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        equip_id = msg.topic.split("/")[1]
        temperatura = float(payload["temperatura"])
        print(f"[LEITURA] {equip_id}: {temperatura:.2f} C")
        processar_leitura(equip_id, temperatura)
    except Exception as e:
        print(f"[ERRO] {msg.topic}: {e}")


def iniciar_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    if MQTT_TLS:
        client.tls_set()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------- calculo de intervalos
def intervalos(equip_id: str, inicio: datetime, fim: datetime) -> list:
    """Segmentos (inicio, fim, ligado) entre inicio e fim, a partir das transicoes."""
    anterior = consultar(
        """
        SELECT timestamp, ligado FROM equipamento_eventos
        WHERE equipamento_id = %s AND timestamp < %s
        ORDER BY timestamp DESC LIMIT 1
        """,
        (equip_id, inicio),
    )
    eventos = consultar(
        """
        SELECT timestamp, ligado FROM equipamento_eventos
        WHERE equipamento_id = %s AND timestamp >= %s AND timestamp < %s
        ORDER BY timestamp ASC
        """,
        (equip_id, inicio, fim),
    )
    rows = anterior + eventos
    segs = []
    for i, (ts, ligado) in enumerate(rows):
        s = max(ts, inicio)
        e = rows[i + 1][0] if i + 1 < len(rows) else fim
        e = min(e, fim)
        if e > s:
            segs.append((s, e, ligado))
    return segs


# ---------------------------------------------------------------- api
@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client
    inicializar_banco()
    carregar_estado_inicial()
    mqtt_client = iniciar_mqtt()
    yield
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


app = FastAPI(title="Monitor de Impressoras 3D", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class Comando(BaseModel):
    acao: str
    valor: Optional[int] = None


@app.get("/api/health")
def health():
    return {"status": "ok", "mqtt_conectado": mqtt_conectado}


@app.get("/api/config")
def config():
    return {
        "temp_liga": TEMP_LIGA,
        "temp_desliga": TEMP_DESLIGA,
        "intervalo_esperado_s": INTERVALO_ESPERADO_S,
    }


@app.get("/api/estado")
def estado():
    ultimos = consultar(
        """
        SELECT DISTINCT ON (equipamento_id) equipamento_id, ligado, timestamp
        FROM equipamento_eventos
        ORDER BY equipamento_id, timestamp DESC
        """
    )
    agora = datetime.now(timezone.utc)
    por_id = {r[0]: {"ligado": r[1], "desde": r[2].isoformat()} for r in ultimos}
    with lock:
        leituras = dict(ultima_leitura)
    ids = sorted(set(por_id) | set(leituras))

    resultado = []
    for equip_id in ids:
        leitura = leituras.get(equip_id)
        online = (
            leitura is not None
            and (agora - leitura["recebido_em"]).total_seconds() <= 2 * INTERVALO_ESPERADO_S
        )
        resultado.append(
            {
                "equipamento_id": equip_id,
                "ligado": por_id.get(equip_id, {}).get("ligado"),
                "desde": por_id.get(equip_id, {}).get("desde"),
                "temperatura": leitura["temperatura"] if leitura else None,
                "ultima_leitura_em": leitura["recebido_em"].isoformat() if leitura else None,
                "segundos_desde_leitura": (
                    round((agora - leitura["recebido_em"]).total_seconds(), 1) if leitura else None
                ),
                "online": online,
            }
        )
    return resultado


@app.get("/api/historico")
def historico(equipamento_id: str, limite: int = Query(50, ge=1, le=500)):
    rows = consultar(
        """
        SELECT id, ligado, temperatura, timestamp FROM equipamento_eventos
        WHERE equipamento_id = %s
        ORDER BY timestamp DESC LIMIT %s
        """,
        (equipamento_id, limite),
    )
    return [
        {
            "id": r[0],
            "ligado": r[1],
            "temperatura": float(r[2]) if r[2] is not None else None,
            "timestamp": r[3].isoformat(),
        }
        for r in rows
    ]


@app.get("/api/timeline")
def timeline(equipamento_id: str, horas: int = Query(24, ge=1, le=168)):
    fim = datetime.now(timezone.utc)
    inicio = fim - timedelta(hours=horas)
    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "segmentos": [
            {"inicio": s.isoformat(), "fim": e.isoformat(), "ligado": lig}
            for s, e, lig in intervalos(equipamento_id, inicio, fim)
        ],
    }


@app.get("/api/tempo-diario")
def tempo_diario(equipamento_id: str, dias: int = Query(7, ge=1, le=31)):
    agora = datetime.now(timezone.utc)
    hoje = agora.astimezone(TZ_LOCAL).date()
    datas = [hoje - timedelta(days=d) for d in range(dias - 1, -1, -1)]
    inicio = datetime.combine(datas[0], dtime.min, tzinfo=TZ_LOCAL)
    segs = intervalos(equipamento_id, inicio, agora)

    resultado = []
    for data in datas:
        d_ini = datetime.combine(data, dtime.min, tzinfo=TZ_LOCAL)
        d_fim = min(d_ini + timedelta(days=1), agora)
        lig = des = 0.0
        for s, e, ligado in segs:
            sobreposicao = (min(e, d_fim) - max(s, d_ini)).total_seconds()
            if sobreposicao > 0:
                if ligado:
                    lig += sobreposicao
                else:
                    des += sobreposicao
        resultado.append(
            {"dia": data.isoformat(), "ligado_s": round(lig), "desligado_s": round(des)}
        )
    return resultado


@app.post("/api/equipamentos/{equipamento_id}/comando")
def enviar_comando(equipamento_id: str, comando: Comando):
    if comando.acao not in ACOES_VALIDAS:
        raise HTTPException(400, f"Acao invalida. Validas: {sorted(ACOES_VALIDAS)}")
    if comando.acao == "set_interval" and (comando.valor is None or comando.valor < 1):
        raise HTTPException(400, "set_interval exige 'valor' em segundos (>= 1)")
    if not mqtt_conectado:
        raise HTTPException(503, "Backend desconectado do broker MQTT")
    payload = {"acao": comando.acao}
    if comando.valor is not None:
        payload["valor"] = comando.valor
    topico = TOPIC_COMANDO.format(equip_id=equipamento_id)
    publicar(topico, payload)
    return {"enviado": True, "topico": topico, **payload}
