import os
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import feedparser
from fast_flights import FlightQuery, create_query, get_flights

# ==============================================================================
# CONFIGURACIÓN ESTRICTA: SOLO TARIFAS ERROR O PRECIOS ULTRA-BAJOS (EZE / AEP)
# ==============================================================================
ORIGINS = ["EZE", "AEP"]

# Umbrales agresivos (Ida y Vuelta en USD final):
# Solo dispara alerta si el precio es MENOR O IGUAL a max_alert_usd
DESTINATIONS = {
    # Estados Unidos
    "MIA": {"name": "Miami 🇺🇸", "normal_usd": 820, "max_alert_usd": 550, "error_usd": 420},
    "JFK": {"name": "Nueva York 🇺🇸", "normal_usd": 920, "max_alert_usd": 600, "error_usd": 450},
    "MCO": {"name": "Orlando 🇺🇸", "normal_usd": 880, "max_alert_usd": 580, "error_usd": 430},
    "LAX": {"name": "Los Ángeles 🇺🇸", "normal_usd": 980, "max_alert_usd": 630, "error_usd": 470},
    # Europa
    "MAD": {"name": "Madrid 🇪🇸", "normal_usd": 1050, "max_alert_usd": 690, "error_usd": 520},
    "BCN": {"name": "Barcelona 🇪🇸", "normal_usd": 1080, "max_alert_usd": 700, "error_usd": 520},
    "FCO": {"name": "Roma 🇮🇹", "normal_usd": 1100, "max_alert_usd": 710, "error_usd": 530},
    "CDG": {"name": "París 🇫🇷", "normal_usd": 1120, "max_alert_usd": 720, "error_usd": 540},
}

ERROR_FARE_FEEDS = [
    "https://promociones-aereas.com.ar/feed",
    "https://www.secretflying.com/posts/category/south-america/feed/",
    "https://www.fly4free.com/flight-deals/south-america/feed/",
]

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_alerts.json"))


def load_seen_alerts():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen_alerts(seen_set):
    try:
        STATE_FILE.write_text(json.dumps(list(seen_set)[-500:]), encoding="utf-8")
    except Exception:
        pass


def get_date_windows(months_ahead=(1, 2, 3, 4, 5, 6, 8, 10), trip_days=14):
    """Genera múltiples ventanas de viaje a lo largo de los próximos 10 meses."""
    today = datetime.now()
    windows = []
    for m in months_ahead:
        dep = today + timedelta(days=30 * m)
        ret = dep + timedelta(days=trip_days)
        windows.append((dep.strftime("%Y-%m-%d"), ret.strftime("%Y-%m-%d")))
    return windows


def format_flight_option(origin, dest_code, dest_info, dep_date, ret_date, flight_obj, url):
    price = flight_obj.price
    airlines = ", ".join(flight_obj.airlines) if flight_obj.airlines else "Varias"
    legs = flight_obj.flights
    stops = max(0, len(legs) - 1)

    if stops == 0:
        stops_str = "Directo (Sin escalas)"
    else:
        layovers = [leg.to_airport.code for leg in legs[:-1]]
        stops_str = f"{stops} escala(s) vía {', '.join(layovers)}"

    total_min = sum(leg.duration for leg in legs if leg.duration)
    hours, mins = divmod(total_min, 60)
    duration_str = f"{hours}h {mins}m" if total_min else "N/D"

    normal = dest_info["normal_usd"]
    discount_pct = round((1 - (price / normal)) * 100)

    if price <= dest_info["error_usd"]:
        badge = f"🚨🚨 ¡TARIFA ERROR DETECTADA! (-{discount_pct}%)"
    else:
        badge = f"🔥 ¡PRECIO BOMBA / MUY BAJO! (-{discount_pct}%)"

    return {
        "id": f"{origin}-{dest_code}-{dep_date}-{price}",
        "origin": origin,
        "dest_code": dest_code,
        "dest_name": dest_info["name"],
        "dep_date": dep_date,
        "ret_date": ret_date,
        "price": price,
        "normal_usd": normal,
        "max_alert_usd": dest_info["max_alert_usd"],
        "discount_pct": discount_pct,
        "airlines": airlines,
        "stops_str": stops_str,
        "duration_str": duration_str,
        "badge": badge,
        "url": url,
        "is_alert": price <= dest_info["max_alert_usd"],
    }


def scan_ultra_low_flights(months_ahead=(1, 2, 4, 6)):
    """Escanea Google Flights y devuelve SOLO los que rompen el piso de precio ultra-bajo."""
    windows = get_date_windows(months_ahead=months_ahead)
    found_deals = []
    all_checked = []

    for dest_code, dest_info in DESTINATIONS.items():
        best_for_dest = None
        for origin in ORIGINS:
            for dep_date, ret_date in windows:
                try:
                    q = create_query(
                        flights=[
                            FlightQuery(date=dep_date, from_airport=origin, to_airport=dest_code),
                            FlightQuery(date=ret_date, from_airport=dest_code, to_airport=origin),
                        ],
                        trip="round-trip",
                        currency="USD",
                    )
                    results = get_flights(q)
                    if not results:
                        continue
                    cheapest = min(results, key=lambda x: x.price if x.price else 999999)
                    if not cheapest or not cheapest.price:
                        continue

                    info = format_flight_option(
                        origin, dest_code, dest_info, dep_date, ret_date, cheapest, q.url()
                    )
                    if best_for_dest is None or info["price"] < best_for_dest["price"]:
                        best_for_dest = info

                    if info["is_alert"]:
                        found_deals.append(info)
                except Exception:
                    continue
        if best_for_dest:
            all_checked.append(best_for_dest)

    return found_deals, all_checked


def scan_strict_error_fare_feeds():
    """
    Filtra estrictamente errores de tarifa o vuelos bomba SOLO desde Buenos Aires (EZE/AEP)
    hacia EE.UU. o Europa, descartando hoteles, paquetes o salidas de otras provincias.
    """
    must_have_bue = ["buenos aires", "ezeiza", "aeroparque", "bue", "eze"]
    exclude_words = [
        "hotel", "hoteles", "alojamiento", "paquete", "paquetes", "all inclusive",
        "desde mendoza", "desde córdoba", "desde cordoba", "desde rosario", "desde santiago", "desde san pablo"
    ]
    dest_keywords = {
        "US": ["miami", "nueva york", "new york", "orlando", "los angeles", "estados unidos", "eeuu", "usa"],
        "EU": ["madrid", "barcelona", "roma", "paris", "parís", "londres", "europa", "italia", "españa"],
    }

    strict_alerts = []
    for feed_url in ERROR_FARE_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                t_low = title.lower()

                if any(ex in t_low for ex in exclude_words):
                    continue
                if not any(b in t_low for b in must_have_bue):
                    continue

                region = None
                if any(k in t_low for k in dest_keywords["US"]):
                    region = "US"
                elif any(k in t_low for k in dest_keywords["EU"]):
                    region = "EU"
                if not region:
                    continue

                # Si menciona un precio en USD, verificar que sea realmente bajo
                usd_match = re.search(r"(?:u\$d|usd|\$)\s*(\d[\d\.]*)", t_low)
                if usd_match:
                    raw_num = usd_match.group(1).replace(".", "")
                    if raw_num.isdigit():
                        price_val = int(raw_num)
                        max_allowed = 600 if region == "US" else 750
                        if price_val > max_allowed and "error" not in t_low:
                            continue

                strict_alerts.append({"id": link, "title": title, "link": link})
        except Exception:
            continue
    return strict_alerts


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    return resp.json()


def auto_discover_chat_id(token):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, timeout=10).json()
    for item in reversed(resp.get("result", [])):
        msg = item.get("message") or item.get("channel_post")
        if msg and "chat" in msg:
            return str(msg["chat"]["id"])
    return None


def run_check(force_summary=False):
    """Ejecuta el chequeo y SOLO envía mensaje si hay una verdadera ganga/error fare."""
    seen = load_seen_alerts()
    deals, all_checked = scan_ultra_low_flights()
    feed_deals = scan_strict_error_fare_feeds()

    new_deals = [d for d in deals if d["id"] not in seen]
    new_feeds = [f for f in feed_deals if f["id"] not in seen]

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and not chat_id:
        chat_id = auto_discover_chat_id(token)

    if not new_deals and not new_feeds and not force_summary:
        print("Escaneo completado: No hay tarifas error ni precios ultra-bajos en este momento. Silencio mantenido.")
        for c in sorted(all_checked, key=lambda x: x["price"]):
            print(f"  - {c['dest_name']}: USD ${c['price']} (Umbral alerta: <= USD ${c['max_alert_usd']})")
        return {"status": "no_deals", "checked": len(all_checked)}

    items_to_show = new_deals if new_deals else (all_checked if force_summary else [])
    header = (
        "🚨 *¡ALERTA DE VUELO BARATO / TARIFA ERROR!* 🚨\n"
        if new_deals or new_feeds
        else "✈️ *REPORTE MANUAL DE PRECIOS ACTUALES (Sin tarifas error hoy)*\n"
    )
    lines = [header]
    for f in sorted(items_to_show, key=lambda x: x["price"]):
        lines.append(
            f"{f['badge'] if f['is_alert'] else '📊 Precio actual'}\n"
            f"📍 *{f['origin']} ➔ {f['dest_name']}* (`{f['dest_code']}`)\n"
            f"💵 Precio I/V: *USD ${f['price']}* _(Umbral alerta: ≤ ${f['max_alert_usd']})_\n"
            f"📅 Fechas: `{f['dep_date']}` al `{f['ret_date']}`\n"
            f"🏢 Aerolínea: {f['airlines']}\n"
            f"🛑 Escalas: {f['stops_str']} ({f['duration_str']} ida)\n"
            f"🔗 [¡Comprar / Ver en Google Flights!]({f['url']})\n"
        )
        if f["is_alert"]:
            seen.add(f["id"])

    if new_feeds:
        lines.append("⚡ *Tarifas Error / Alertas desde Buenos Aires:*")
        for a in new_feeds:
            lines.append(f"• [{a['title']}]({a['link']})")
            seen.add(a["id"])

    save_seen_alerts(seen)
    msg_text = "\n".join(lines)
    print(msg_text)

    if token and chat_id:
        send_telegram_message(token, chat_id, msg_text)
        print(f"Alerta enviada a Telegram ({chat_id}).")

    return {"status": "alert_sent", "deals": len(new_deals), "feeds": len(new_feeds)}


class CloudRunHandler(BaseHTTPRequestHandler):
    """Servidor HTTP liviano para Google Cloud Run + Cloud Scheduler."""

    def do_GET(self):
        result = run_check(force_summary=False)
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()


if __name__ == "__main__":
    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
        print(f"Iniciando servidor en puerto {port} para Google Cloud Run...")
        HTTPServer(("0.0.0.0", port), CloudRunHandler).serve_forever()
    else:
        force = os.environ.get("FORCE_SUMMARY", "false").lower() == "true"
        run_check(force_summary=force)
