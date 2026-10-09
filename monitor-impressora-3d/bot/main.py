import os
import json
import asyncio
from datetime import datetime

import discord
from discord.ext import commands
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1418010255488581704"))

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TLS = os.getenv("MQTT_TLS", "true").lower() == "true"

TOPIC_STATUS = "equipamentos/+/status"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

loop_ref: asyncio.AbstractEventLoop | None = None
mqtt_client: mqtt.Client | None = None


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT] Conexao: {reason_code}")
    if not reason_code.is_failure:
        client.subscribe(TOPIC_STATUS, qos=1)


def on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        equip_id = payload["equipamento_id"]
        status = payload["status"]  # "ligado" | "desligado"
        timestamp = datetime.fromisoformat(payload["timestamp"])
        data_hora = timestamp.astimezone().strftime("%d/%m/%Y %H:%M:%S")

        texto = f"{data_hora} - {equip_id.upper()} {'LIGADO' if status == 'ligado' else 'DESLIGADO'}"
        if payload.get("temperatura") is not None:
            texto += f" ({payload['temperatura']:.1f} °C)"

        if loop_ref:
            asyncio.run_coroutine_threadsafe(enviar_mensagem(texto), loop_ref)
    except Exception as e:
        print(f"[ERRO] Falha ao processar status MQTT: {e}")


async def enviar_mensagem(texto: str):
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("[DISCORD] Canal nao encontrado")
        return
    await channel.send(texto)
    print(f"[DISCORD] {texto}")


def iniciar_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="discord-bot")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    if MQTT_TLS:
        client.tls_set()
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()  # thread propria, nao bloqueia o discord.py
    return client


@bot.event
async def on_ready():
    global loop_ref, mqtt_client
    loop_ref = asyncio.get_running_loop()
    print(f"Bot online como {bot.user}")
    # on_ready dispara de novo a cada reconexao com o Discord.
    # Sem esta guarda, um segundo cliente com o mesmo client_id
    # derrubaria o primeiro no broker.
    if mqtt_client is None:
        mqtt_client = iniciar_mqtt()


if __name__ == "__main__":
    bot.run(TOKEN)
