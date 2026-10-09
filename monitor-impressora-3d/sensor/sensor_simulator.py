"""
Simulador do ESP32 + sensor de temperatura.

Edge (borda):
- Coleta N amostras por ciclo, descarta leituras invalidas e publica a media.

Cloud -> Edge:
- Assina equipamentos/<id>/comando e executa:
    led_on / led_off      -> liga/desliga o LED (simulado no terminal)
    read_now              -> faz uma leitura imediata
    set_interval {valor}  -> altera o intervalo de coleta (segundos)

Temperatura simulada em rampa: aquece ate TEMP_ALVO_QUENTE, mantem,
resfria ate TEMP_ALVO_FRIO, mantem, repete. Gera transicoes previsiveis.
"""
import os
import json
import random
import threading

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TLS = os.getenv("MQTT_TLS", "true").lower() == "true"

EQUIP_ID = os.getenv("EQUIP_ID", "impressora-1")
intervalo_s = int(os.getenv("INTERVALO_S", 10))

TEMP_ALVO_QUENTE = float(os.getenv("TEMP_ALVO_QUENTE", 60))
TEMP_ALVO_FRIO = float(os.getenv("TEMP_ALVO_FRIO", 25))
PASSO_C = float(os.getenv("PASSO_C", 4))        # variacao por ciclo
CICLOS_PATAMAR = int(os.getenv("CICLOS_PATAMAR", 6))  # ciclos parado no alvo

AMOSTRAS_POR_CICLO = 5
FAIXA_VALIDA = (-10.0, 125.0)  # faixa fisica de um sensor digital comum
PROB_LEITURA_INVALIDA = 0.05   # simula falha de leitura (ex.: -127 do DS18B20)

TOPIC_TELEMETRIA = f"equipamentos/{EQUIP_ID}/temperatura"
TOPIC_COMANDO = f"equipamentos/{EQUIP_ID}/comando"

acordar = threading.Event()
led_ligado = False


class Termica:
    """Modelo simples da temperatura da impressora."""

    def __init__(self):
        self.temp = TEMP_ALVO_FRIO
        self.aquecendo = True
        self.patamar = 0

    def avancar(self) -> float:
        alvo = TEMP_ALVO_QUENTE if self.aquecendo else TEMP_ALVO_FRIO
        if abs(self.temp - alvo) <= PASSO_C:
            self.temp = alvo
            self.patamar += 1
            if self.patamar >= CICLOS_PATAMAR:
                self.aquecendo = not self.aquecendo
                self.patamar = 0
        else:
            self.temp += PASSO_C if self.temp < alvo else -PASSO_C
        return self.temp


def ler_sensor(temp_real: float) -> float:
    if random.random() < PROB_LEITURA_INVALIDA:
        return -127.0
    return temp_real + random.gauss(0, 0.6)


def processar_na_borda(temp_real: float):
    """Coleta N amostras, descarta invalidas e retorna a media."""
    amostras = [ler_sensor(temp_real) for _ in range(AMOSTRAS_POR_CICLO)]
    validas = [a for a in amostras if FAIXA_VALIDA[0] <= a <= FAIXA_VALIDA[1]]
    descartadas = len(amostras) - len(validas)
    if not validas:
        return None, descartadas
    return round(sum(validas) / len(validas), 2), descartadas


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT] Conexao: {reason_code}")
    if not reason_code.is_failure:
        client.subscribe(TOPIC_COMANDO, qos=1)


def on_message(client, userdata, msg):
    global led_ligado, intervalo_s
    try:
        cmd = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        print(f"[CMD] Payload invalido: {msg.payload!r}")
        return

    acao = cmd.get("acao")
    if acao == "led_on":
        led_ligado = True
        print("[CMD] LED ACESO")
    elif acao == "led_off":
        led_ligado = False
        print("[CMD] LED APAGADO")
    elif acao == "read_now":
        print("[CMD] Leitura imediata solicitada")
        acordar.set()
    elif acao == "set_interval":
        try:
            novo = int(cmd["valor"])
            if novo < 1:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            print(f"[CMD] set_interval invalido: {cmd}")
            return
        intervalo_s = novo
        print(f"[CMD] Intervalo alterado para {intervalo_s}s")
        acordar.set()
    else:
        print(f"[CMD] Acao desconhecida: {cmd}")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sensor-{EQUIP_ID}")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    if MQTT_TLS:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    termica = Termica()
    try:
        while True:
            temp_real = termica.avancar()
            media, descartadas = processar_na_borda(temp_real)
            led = "ON " if led_ligado else "OFF"
            if media is None:
                print(f"[EDGE] Todas as amostras invalidas, ciclo descartado | LED {led}")
            else:
                payload = json.dumps({"temperatura": media})
                client.publish(TOPIC_TELEMETRIA, payload, qos=1)
                print(
                    f"[ENVIADO] {TOPIC_TELEMETRIA} -> {payload} "
                    f"| descartadas={descartadas} | LED {led}"
                )
            acordar.wait(intervalo_s)
            acordar.clear()
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
