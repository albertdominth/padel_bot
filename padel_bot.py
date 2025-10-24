import json
from datetime import datetime, timedelta
import re
import requests
import subprocess
import os
import pytz
import time
import sys

# === CONFIGURACIÓN ===
os.environ["TZ"] = "Europe/Madrid"
time.tzset()

URL = "https://www.padelcpi.com/booking/srvc.aspx/ObtenerCuadro"
GRID_URL = "https://www.padelcpi.com/Booking/Grid.aspx"
PISTAS_FILE = "pistas.json"
PISTAS_ACTUALES_FILE = "pistas_actuales.json"

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://www.padelcpi.com",
    "referer": "https://www.padelcpi.com/Booking/Grid.aspx",
    "user-agent": "Mozilla/5.0"
}

COOKIES = {
    "cb-enabled": "enabled",
    "MPOpcionCookie": "necesarios",
    "ASP.NET_SessionId": "1uoyuc45nc2ljibx5plcoxav",  # ⚠️ cámbialo si tu sesión cambia
    "i18next": "ca-ES"
}

DURACION_MINUTOS = 90
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# === TOKEN DINÁMICO ===
def obtener_token():
    curl_html = [
        "curl", "-s",
        GRID_URL,
        "-H", "accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "-H", "user-agent: Mozilla/5.0",
        "-b", "cb-enabled=enabled; MPOpcionCookie=necesarios; ASP.NET_SessionId=1uoyuc45nc2ljibx5plcoxav; i18next=ca-ES"
    ]
    html_result = subprocess.run(curl_html, capture_output=True, text=True)
    html_text = html_result.stdout

    match = re.search(r"hl90njda2b89k\s*=\s*'([^']+)'", html_text)
    if not match:
        raise RuntimeError("❌ No se pudo encontrar el token dinámico en el HTML de Grid.aspx")

    return match.group(1)


# === FUNCIONES AUXILIARES ===
def parse_ms_date(ms_date):
    if isinstance(ms_date, datetime):
        return ms_date
    if ms_date is None:
        return None
    s = str(ms_date)
    m = re.search(r"(-?\d+)", s)
    if not m:
        return None
    ts_ms = int(m.group(1))
    return datetime.fromtimestamp(ts_ms / 1000)


def parse_str_hora(fecha_base, str_hora):
    hora, minuto = map(int, str_hora.split(":"))
    return fecha_base.replace(hour=hora, minute=minuto, second=0, microsecond=0)


def merge_intervals(intervals):
    if not intervals:
        return []
    intervals_sorted = sorted(intervals, key=lambda x: x[0])
    merged = []
    cur_start, cur_end = intervals_sorted[0]
    for s, e in intervals_sorted[1:]:
        if s <= cur_end + timedelta(seconds=1):
            cur_end = max(cur_end, e)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    merged.append((cur_start, cur_end))
    return merged


def buscar_huecos(json_data, franja_inicio, franja_fin, duracion_min=DURACION_MINUTOS):
    data = json_data.get("d", {})
    fecha_str = data.get("StrFecha")
    if not fecha_str:
        return []

    fecha_base = datetime.strptime(fecha_str, "%d/%m/%Y")
    hora_inicio_franja = parse_str_hora(fecha_base, franja_inicio)
    hora_fin_franja = parse_str_hora(fecha_base, franja_fin)
    duracion = timedelta(minutes=duracion_min)

    huecos_totales = []

    for columna in data.get("Columnas", []):
        nombre_pista = columna.get("TextoPrincipal", "sin nombre")
        ocupaciones = []

        for o in columna.get("Ocupaciones", []):
            inicio_raw = o.get("HoraInicio")
            fin_raw = o.get("HoraFin")
            inicio = parse_ms_date(inicio_raw)
            fin = parse_ms_date(fin_raw)

            if inicio is None and o.get("StrHoraInicio"):
                inicio = parse_str_hora(fecha_base, o["StrHoraInicio"])
            if fin is None and o.get("StrHoraFin"):
                fin = parse_str_hora(fecha_base, o["StrHoraFin"])
            if not inicio or not fin:
                continue

            if fin <= hora_inicio_franja or inicio >= hora_fin_franja:
                continue

            inicio_clamped = max(inicio, hora_inicio_franja)
            fin_clamped = min(fin, hora_fin_franja)
            if inicio_clamped < fin_clamped:
                ocupaciones.append((inicio_clamped, fin_clamped))

        ocupaciones_merged = merge_intervals(ocupaciones)
        cursor = hora_inicio_franja

        for s, e in ocupaciones_merged:
            if s > cursor and (s - cursor) >= duracion:
                huecos_totales.append((nombre_pista, cursor, s))
            cursor = max(cursor, e)

        if hora_fin_franja > cursor and (hora_fin_franja - cursor) >= duracion:
            huecos_totales.append((nombre_pista, cursor, hora_fin_franja))

    return huecos_totales


def obtener_franja_por_dia(dia_semana):
    if dia_semana in range(0, 4):  # lunes a jueves
        return "18:30", "21:30"
    elif dia_semana == 4:  # viernes
        return "15:30", "18:00"
    else:
        return None, None


# === UTILIDADES JSON ===
def cargar_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def guardar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resultados_a_dict(lista_huecos):
    salida = {}
    for dia, huecos in lista_huecos.items():
        salida[dia] = [
            {"pista": pista, "inicio": inicio.strftime("%Y-%m-%d %H:%M"), "fin": fin.strftime("%Y-%m-%d %H:%M")}
            for pista, inicio, fin in huecos
        ]
    return salida


# === TELEGRAM ===
def enviar_telegram(mensaje):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML"}

    try:
        r = requests.post(url, data=payload, timeout=10)
        if not r.ok:
            print(f"⚠️ Error al enviar mensaje: {r.text}")
    except Exception as e:
        print(f"❌ Error enviando Telegram: {e}")


# === GIT COMMIT / PUSH ===
def git_commit_and_push(commit_message):
    """Hace commit y push automático al repositorio actual."""
    try:
        subprocess.run(["git", "config", "user.name", "Padel Bot"], check=True)
        subprocess.run(["git", "config", "user.email", "padel-bot@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "pistas.json", "pistas_actuales.json"], check=True)
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if result.stdout.strip() == "":
            print("ℹ️ No hay cambios que commitear.")
            return
        subprocess.run(["git", "commit", "-m", commit_message], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ Cambios subidos al repositorio correctamente.")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Error al hacer commit/push: {e}")


# === MAIN ===
if __name__ == "__main__":
    TOKEN = obtener_token()
    resultados_actuales = {}
    hoy = datetime.now().date()

    for i in range(0, 4):
        fecha_obj = hoy + timedelta(days=i)
        dia_semana_num = fecha_obj.weekday()
        dia_semana = DIAS_ES[dia_semana_num].capitalize()

        franja_inicio, franja_fin = obtener_franja_por_dia(dia_semana_num)
        if not franja_inicio:
            continue

        DATA = {
            "idCuadro": 4,
            "fecha": fecha_obj.strftime("%d/%m/%Y"),
            "key": TOKEN
        }

        try:
            response = requests.post(URL, headers=HEADERS, cookies=COOKIES, json=DATA, timeout=20)
            response.raise_for_status()
            data = response.json()
            huecos = buscar_huecos(data, franja_inicio, franja_fin)
            if huecos:
                resultados_actuales[f"{dia_semana} {fecha_obj.strftime('%d/%m/%Y')}"] = huecos
        except Exception as e:
            print(f"⚠️ Error al procesar {fecha_obj}: {e}")

    resultados_dict = resultados_a_dict(resultados_actuales)

    guardar_json(PISTAS_ACTUALES_FILE, resultados_dict)
    pistas_previas = cargar_json(PISTAS_FILE)

    nuevas_pistas = {}
    for dia, huecos in resultados_dict.items():
        prev_huecos = pistas_previas.get(dia, [])
        prev_set = {(h["pista"], h["inicio"], h["fin"]) for h in prev_huecos}
        act_set = {(h["pista"], h["inicio"], h["fin"]) for h in huecos}
        nuevas = act_set - prev_set
        if nuevas:
            nuevas_pistas[dia] = list(nuevas)

    if nuevas_pistas:
        mensaje = "🎾 <b>Nuevas pistas disponibles</b>\n"
        for dia, pistas in nuevas_pistas.items():
            mensaje += f"\n📅 <b>{dia}</b>\n"
            for pista, inicio, fin in pistas:
                mensaje += f"  🟢 {pista}: {inicio[-5:]} - {fin[-5:]}\n"
        print(mensaje)
        enviar_telegram(mensaje)

    if resultados_dict != pistas_previas:
        guardar_json(PISTAS_FILE, resultados_dict)
        git_commit_and_push("🧩 Actualización automática de pistas disponibles")
