import os
import time
import json
import threading
import requests
from flask import Flask, render_template_string, jsonify, request
from supabase import create_client, Client

# =======================================================
# 1. CREDENCIALES Y CONFIGURACIÓN
# =======================================================
TELEGRAM_BOT_TOKEN = "8906055800:AAHvhYu4e8v2qCy9dOIt51MSKmK_Ku4mewE"
TELEGRAM_CHAT_ID = "8749748612"

# Clave de Google AI Studio
GEMINI_API_KEY = "AQ.Ab8RN6LM2d0vDcuV8codC9tUHIlpz_XezEpzBDCJu3Y1cu5r0A"

# Credenciales de Supabase
SUPABASE_URL = "https://wwswaqbpookqidqkbknf.supabase.co"
SUPABASE_KEY = "sb_secret_vYT0rSqyIfu2QufzCG-iKw_mCGyVd_l"
TABLA_PRODUCTOS = "products"

# Capacidad física máxima del almacén (suficiente para todo el catálogo)
LIMITE_ALMACEN_TOTAL = 3000

# =======================================================
# 2. CLIENTE SUPABASE CON DETECCIÓN DINÁMICA
# =======================================================
app = Flask(__name__)
supabase: Client = None
supabase_conectado = False
columna_stock_detectada = None

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase_conectado = True
    print(f"[Supabase] Conectado exitosamente.")
    
    # Inspeccionar columnas existentes en la tabla
    muestra = supabase.table(TABLA_PRODUCTOS).select("*").limit(1).execute()
    if muestra.data:
        columnas = set(muestra.data[0].keys())
        print(f"[Supabase] Columnas en '{TABLA_PRODUCTOS}': {list(columnas)}")
        for col in ["stock_actual", "stock", "units", "quantity", "inventory"]:
            if col in columnas:
                columna_stock_detectada = col
                break
        print(f"[Supabase] Columna de stock asignada: {columna_stock_detectada or 'En memoria (sin columna física)'}")
except Exception as e:
    print(f"[Supabase] Error al inicializar: {e}")

LEAD_TIMES = {
    "SUP01": 1, "SUP02": 3, "SUP03": 2, "SUP04": 1,
    "SUP05": 7, "SUP06": 2, "SUP07": 3, "SUP08": 2
}

stock_en_memoria = {}
descuentos_aprobados = {}

# =======================================================
# CONFIGURACIÓN INTERNA DE VENTAS (5 DE MENOR VENTA)
# =======================================================
VENTAS_ALTAS = {
    "SKU-102560": 85,
    "SKU-100001": 62,
    "SKU-100002": 54
}

# Exactamente 5 productos con menor salida para Telegram
VENTAS_BAJAS_5 = {
    "SKU-100003": 1,
    "SKU-100004": 2,
    "SKU-100005": 1,
    "SKU-100006": 3,
    "SKU-100007": 2
}

# =======================================================
# 3. ESTADO GLOBAL (TELEMETRÍA)
# =======================================================
estado_lock = threading.Lock()

estado = {
    "stock_anaquel": 8,
    "peso_gramos": 800.0,
    "total_alertas": 0,
    "ultimo_evento": "Conectado a Supabase",
    "recomendacion_ia": "Esperando evaluación de compras de Glazed Co..."
}

# =======================================================
# 4. FUNCIONES DE BASE DE DATOS Y CAPACIDAD
# =======================================================
def evaluar_descuento(sku, stock, shelf_life, precio_base, costo):
    sku_str = str(sku).upper()
    
    ventas = None
    es_baja = False
    
    for k, v in VENTAS_BAJAS_5.items():
        if k.upper() in sku_str or sku_str.endswith(k.upper().replace("SKU-", "")):
            ventas = v
            es_baja = True
            break
            
    if ventas is None:
        for k, v in VENTAS_ALTAS.items():
            if k.upper() in sku_str or sku_str.endswith(k.upper().replace("SKU-", "")):
                ventas = v
                es_baja = False
                break

    if ventas is None:
        ventas = 15

    pct = 0
    motivo = "Precio Regular"

    if es_baja:
        pct = 25  # 25% sugerido en Telegram
        motivo = f"Baja Venta ({ventas} uds)"
    elif stock >= 10 and shelf_life <= 2:
        pct = 35
        motivo = "Caducidad Inminente"
    elif stock >= 6 and shelf_life <= 4:
        pct = 20
        motivo = "Baja Rotación"
    elif stock > 15:
        pct = 10
        motivo = "Exceso de Stock"

    p_desc = round(precio_base * (1.0 - (pct / 100.0)), 2)
    if p_desc < costo:
        p_desc = round(costo * 1.05, 2)
        pct = max(0, int(round((1.0 - (p_desc / precio_base)) * 100)))

    return pct, p_desc, motivo, ventas, es_baja

def normalizar_producto(p):
    sku = str(p.get("sku") or p.get("id") or "SKU-GEN")
    supplier = str(p.get("supplier_id") or p.get("supplier") or "SUP01")
    case_size = int(p.get("units_per_case") or p.get("case_size") or 10)
    
    if columna_stock_detectada and p.get(columna_stock_detectada) is not None:
        try:
            stk = int(p.get(columna_stock_detectada))
        except:
            stk = stock_en_memoria.get(sku, 10)
    else:
        stk = stock_en_memoria.get(sku, 10)

    stock_en_memoria[sku] = stk

    precio = float(p.get("list_price_base") or p.get("precio") or 2.5)
    costo = float(p.get("unit_cost") or p.get("costo_unitario") or 1.0)
    shelf_life = int(p.get("shelf_life_days") or 2)

    pct_sug, p_sug, motivo, ventas, es_baja = evaluar_descuento(sku, stk, shelf_life, precio, costo)
    aprobado = descuentos_aprobados.get(sku, False)
    precio_final = p_sug if aprobado else precio

    return {
        "sku": sku,
        "nombre": str(p.get("product_name") or p.get("nombre") or sku),
        "stock_actual": stk,
        "lead_time_dias": int(p.get("lead_time_days") or p.get("lead_time_dias") or LEAD_TIMES.get(supplier, 2)),
        "shelf_life_days": shelf_life,
        "case_size": case_size,
        "costo_unitario": costo,
        "precio": precio,
        "precio_final": precio_final,
        "ventas_semana": ventas,
        "es_baja_venta": es_baja,
        "descuento_sugerido_pct": pct_sug,
        "precio_sugerido": p_sug,
        "motivo_descuento": motivo,
        "descuento_aprobado": aprobado,
        "supplier_id": supplier,
        "status": str(p.get("status") or "Active")
    }

def obtener_catalogo_supabase():
    if not (supabase_conectado and supabase):
        return []
    try:
        res = supabase.table(TABLA_PRODUCTOS).select("*").execute()
        if res.data:
            return [normalizar_producto(item) for item in res.data]
    except Exception as e:
        print(f"[Supabase] Error en consulta select: {e}")
    return []

def actualizar_stock_seguro(sku, nuevo_stock):
    stock_en_memoria[sku] = nuevo_stock
    if supabase_conectado and supabase and columna_stock_detectada:
        try:
            supabase.table(TABLA_PRODUCTOS).update({
                columna_stock_detectada: nuevo_stock
            }).eq("sku", sku).execute()
        except Exception as e:
            print(f"[Supabase] Error escribiendo en tabla: {e}")
    return True

def calcular_ocupacion_almacen():
    catalogo = obtener_catalogo_supabase()
    total_unidades = sum(p["stock_actual"] for p in catalogo)
    espacio_libre = max(0, LIMITE_ALMACEN_TOTAL - total_unidades)
    porcentaje_uso = (total_unidades / LIMITE_ALMACEN_TOTAL) * 100 if LIMITE_ALMACEN_TOTAL > 0 else 0
    return total_unidades, espacio_libre, porcentaje_uso

def conmutar_descuento_producto(sku, activar=True):
    catalogo = obtener_catalogo_supabase()
    prod = next((p for p in catalogo if p["sku"].upper() == str(sku).upper()), None)
    if not prod:
        return False, "Producto no encontrado."

    if activar:
        descuentos_aprobados[prod["sku"]] = True
        msg = f"🏷️ *Descuento Aprobado:* {prod['nombre']} (`{prod['sku']}`) con -{prod['descuento_sugerido_pct']}% (Nuevo precio: ${prod['precio_sugerido']:.2f})."
    else:
        descuentos_aprobados.pop(prod["sku"], None)
        msg = f"⚪ *Precio Regular:* {prod['nombre']} (`{prod['sku']}`) se mantiene sin oferta (${prod['precio']:.2f})."

    with estado_lock:
        estado["ultimo_evento"] = f"Oferta: {prod['sku']} ({'Activa' if activar else 'Normal'})"

    threading.Thread(target=enviar_telegram_chat, args=(TELEGRAM_CHAT_ID, msg), daemon=True).start()
    return True, msg

# =======================================================
# 5. PREDICCIÓN CON GEMINI FLASH (HEADER x-goog-api-key)
# =======================================================
def analizar_probabilidad_semanal_gemini():
    catalogo = obtener_catalogo_supabase()[:10]
    if not catalogo:
        return "No hay productos en Supabase para evaluar."

    resumen_productos = []
    for p in catalogo:
        resumen_productos.append(
            f"SKU: {p['sku']} | Producto: {p['nombre']} | Stock: {p['stock_actual']} | "
            f"Lead Time: {p['lead_time_dias']}d | Vida Útil: {p['shelf_life_days']}d | "
            f"Case Size: {p['case_size']}"
        )

    prompt = (
        "Eres el director de analítica y compras de 'Glazed Co.' en un punto de venta de alto tránsito.\n"
        "Considera el ciclo comercial semanal (alta afluencia de jueves a domingo):\n"
        + "\n".join(resumen_productos) + "\n\n"
        "Calcula la probabilidad de desabasto para los próximos 7 días y la fecha de reorden al proveedor. "
        "Devuelve un reporte conciso de 3 a 4 puntos con: SKU/Producto, Probabilidad de quiebre (%), "
        "Día recomendado para colocar la orden y cantidad de compra sugerida en múltiplos exactos de case_size para evitar mermas. "
        "Sin asteriscos excesivos ni formato complejo."
    )

    modelos = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"]
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2}
    }

    for mod in modelos:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mod}:generateContent"
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=8)
            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip().replace("*", "")
            else:
                print(f"[Gemini API] Error {mod} (HTTP {r.status_code}): {r.text[:120]}")
        except Exception as e:
            print(f"[Gemini API] Excepción al conectar con {mod}: {e}")

    # Fallback estadístico local si la red falla
    criticos = sorted(catalogo, key=lambda x: x["stock_actual"])[:3]
    dias = ["Jueves", "Viernes", "Sábado"]
    rep = "📊 ANÁLISIS DE DEMANDA SEMANAL (MOTOR LOCAL GLAZED CO.)\n\n"
    for i, p in enumerate(criticos):
        stk = p["stock_actual"]
        prob = 90 if stk <= 2 else (70 if stk <= 5 else 40)
        cajas = 1 if p["shelf_life_days"] <= 2 else 2
        cant = cajas * p["case_size"]
        rep += (
            f"• {p['sku']} - {p['nombre']}:\n"
            f"   Riesgo de quiebre: {prob}% | Día sugerido: {dias[i % len(dias)]}\n"
            f"   Sugerencia: Solicitar {cant} uds ({cajas} cajas) evitando merma.\n\n"
        )
    return rep.strip()

# =======================================================
# 6. ÓRDENES DE REORDEN CON CONTROL DE ALMACÉN Y BÚSQUEDA FLEXIBLE
# =======================================================
def procesar_confirmacion_pedido(sku_objetivo=None, cantidad_manual=None, origen="Telegram"):
    catalogo = obtener_catalogo_supabase()
    if not catalogo:
        return "❌ Error: La base de datos en Supabase está vacía."

    total_actual, espacio_libre, pct = calcular_ocupacion_almacen()

    prod_seleccionado = None
    if sku_objetivo:
        sku_clean = str(sku_objetivo).upper().strip()
        for p in catalogo:
            p_sku = str(p["sku"]).upper()
            if sku_clean == p_sku or p_sku.endswith(sku_clean) or sku_clean in p_sku:
                prod_seleccionado = p
                break
    else:
        prod_seleccionado = min(catalogo, key=lambda x: x["stock_actual"])

    if not prod_seleccionado:
        return f"❌ No se encontró ningún producto que coincida con '{sku_objetivo}'."

    sku = prod_seleccionado["sku"]
    nombre = prod_seleccionado["nombre"]
    stock_anterior = prod_seleccionado["stock_actual"]
    case_size = prod_seleccionado["case_size"]
    shelf_life = prod_seleccionado["shelf_life_days"]
    lead_time = prod_seleccionado["lead_time_dias"]
    costo_unit = prod_seleccionado["costo_unitario"]

    if cantidad_manual is not None and int(cantidad_manual) > 0:
        cant_pedir = int(cantidad_manual)
    else:
        cajas = 1 if stock_anterior > 0 and shelf_life <= 2 else 2
        cant_pedir = cajas * case_size

    if espacio_libre <= 0:
        return (
            f"🚫 *ALMACÉN LLENO*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ocupación: `{total_actual}/{LIMITE_ALMACEN_TOTAL} uds` (100% lleno).\n"
            f"No caben más unidades en el almacén físico."
        )

    if cant_pedir > espacio_libre:
        cant_pedir = espacio_libre

    nuevo_stock = stock_anterior + cant_pedir
    inversion = cant_pedir * costo_unit

    actualizar_stock_seguro(sku, nuevo_stock)
    nuevo_total, nuevo_libre, nuevo_pct = calcular_ocupacion_almacen()

    with estado_lock:
        estado["stock_anaquel"] = nuevo_stock
        estado["ultimo_evento"] = f"Reorden ({origen}): +{cant_pedir} uds ({sku})"

    return (
        f"✅ *ORDEN PROCESADA Y REGISTRADA*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Producto:* {nombre} (`{sku}`)\n"
        f"🔢 *Cantidad surtida:* `+{cant_pedir} uds`\n"
        f"📊 *Stock producto:* `{stock_anterior}` ➔ `{nuevo_stock} uds`\n"
        f"🏢 *Ocupación Almacén:* `{nuevo_total}/{LIMITE_ALMACEN_TOTAL} uds` ({nuevo_pct:.1f}%)\n"
        f"📦 *Espacio libre:* `{nuevo_libre} uds`\n"
        f"💰 *Costo estimado:* `${inversion:.2f} USD`\n"
        f"🚚 *Lead Time:* `{lead_time} día(s)`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 _Sincronizado con Supabase._"
    )

# =======================================================
# 7. BOT DE TELEGRAM CON COMANDO /novendidos
# =======================================================
def enviar_telegram_chat(chat_id, mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram] Error enviando mensaje: {e}")

def enviar_telegram_con_botones(chat_id, mensaje, botones=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "Markdown"}
    if botones:
        payload["reply_markup"] = json.dumps({"inline_keyboard": botones})
    try:
        res = requests.post(url, json=payload, timeout=6)
        if res.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=6)
    except Exception as e:
        print(f"[Telegram] Error enviando botones: {e}")

def responder_callback(callback_query_id, texto_alerta):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id, "text": texto_alerta}, timeout=4)
    except Exception:
        pass

def enviar_5_no_vendidos_telegram(chat_id):
    catalogo = obtener_catalogo_supabase()
    if not catalogo:
        enviar_telegram_chat(chat_id, "⚠️ No se encontraron productos en la base de datos de Supabase.")
        return

    no_vendidos = [p for p in catalogo if p.get("es_baja_venta")]
    
    if len(no_vendidos) < 5:
        ordenados = sorted(catalogo, key=lambda x: x.get("ventas_semana", 15))
        for p in ordenados:
            if p not in no_vendidos:
                p["descuento_sugerido_pct"] = p.get("descuento_sugerido_pct") or 25
                p["precio_sugerido"] = round(p["precio"] * 0.75, 2)
                no_vendidos.append(p)
            if len(no_vendidos) == 5:
                break

    no_vendidos = no_vendidos[:5]

    enviar_telegram_chat(
        chat_id, 
        f"📉 *LOS 5 PRODUCTOS DE MENOR VENTA*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Productos con baja demanda. Selecciona si deseas ponerlos en oferta o dejarlos a precio normal:"
    )

    for p in no_vendidos:
        sku = p["sku"]
        pct = p.get("descuento_sugerido_pct") or 25
        p_oferta = p.get("precio_sugerido") or round(p["precio"] * 0.75, 2)
        p_normal = p["precio"]
        aprobado = p.get("descuento_aprobado", False)

        estado_txt = "🟢 *EN OFERTA*" if aprobado else "⚪ *PRECIO REGULAR*"

        msg = (
            f"🍩 *{p['nombre'][:24]}* (`{sku}`)\n"
            f"📊 Ventas: `{p.get('ventas_semana', 1)} uds` | Existencia: `{p['stock_actual']} uds`\n"
            f"💰 Normal: `${p_normal:.2f}` ➔ Oferta: *${p_oferta:.2f}* (-{pct}%)\n"
            f"Estado: {estado_txt}"
        )

        botones = [
            [
                {"text": f"🏷️ Poner en Oferta (-{pct}%)", "callback_data": f"oferta:{sku}"},
                {"text": "⚪ Mantener Normal", "callback_data": f"normal:{sku}"}
            ]
        ]
        enviar_telegram_con_botones(chat_id, msg, botones)

def enviar_ofertas_telegram(chat_id):
    catalogo = obtener_catalogo_supabase()
    con_oferta = [p for p in catalogo if p["descuento_sugerido_pct"] > 0]
    if not con_oferta:
        enviar_telegram_chat(chat_id, "✅ *Todos los productos tienen rotación normal.* No hay ofertas sugeridas.")
        return

    enviar_telegram_chat(chat_id, f"🏷️ *OFERTAS Y DESCUENTOS SUGERIDOS ({len(con_oferta)})*\nSelecciona una acción:")
    for p in con_oferta[:5]:
        sku = p["sku"]
        pct = p["descuento_sugerido_pct"]
        aprobado = p["descuento_aprobado"]
        estado_desc = "🟢 ACTIVO" if aprobado else "⚪ PENDIENTE"

        msg = (
            f"📦 *{p['nombre'][:22]}* (`{sku}`)\n"
            f"Existencia: `{p['stock_actual']} uds` | Motivo: _{p['motivo_descuento']}_\n"
            f"Precio: `${p['precio']:.2f}` ➔ *${p['precio_sugerido']:.2f}* (-{pct}%)\n"
            f"Estado: {estado_desc}"
        )

        botones = [
            [
                {"text": f"🏷️ Poner en Oferta (-{pct}%)", "callback_data": f"oferta:{sku}"},
                {"text": "⚪ Mantener Normal", "callback_data": f"normal:{sku}"}
            ]
        ]
        enviar_telegram_con_botones(chat_id, msg, botones)

def reporte_stock_telegram():
    prods = obtener_catalogo_supabase()
    if not prods:
        return "📦 No se encontraron productos en la tabla 'products' de Supabase."

    txt = f"🍩 *DAI*\n━━━━━━━━━━━━━━━━━━━━\n"
    for p in prods[:10]:
        stk = p["stock_actual"]
        alerta = "🔴 CRÍTICO" if stk <= 2 else ("🟡 REORDENAR" if stk <= 5 else "🟢 ÓPTIMO")
        txt += f"• `{p['sku']}` | *{p['nombre'][:22]}*\n   Existencia: `{stk} uds` | Lead: `{p['lead_time_dias']}d` | {alerta}\n\n"
    txt += "━━━━━━━━━━━━━━━━━━━━\nUsa `/novendidos` para gestionar los 5 de menor rotación."
    return txt

def bucle_escucha_telegram():
    last_update_id = 0
    url_updates = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    print("[Telegram] Bot iniciado y escuchando comandos...")

    while True:
        try:
            params = {"offset": last_update_id + 1, "timeout": 15}
            r = requests.get(url_updates, params=params, timeout=20)
            if r.status_code == 200:
                data = r.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]

                    # 1. Clics en los botones inline
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        cb_data = cb.get("data", "")
                        c_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))

                        if cb_data.startswith("oferta:"):
                            sku = cb_data.split("oferta:")[1]
                            ok, det = conmutar_descuento_producto(sku, activar=True)
                            responder_callback(cb_id, f"Oferta aplicada a {sku}")
                            enviar_telegram_chat(c_id, f"✅ *Confirmado:* {det}")

                        elif cb_data.startswith("normal:"):
                            sku = cb_data.split("normal:")[1]
                            ok, det = conmutar_descuento_producto(sku, activar=False)
                            responder_callback(cb_id, f"Precio normal conservado para {sku}")
                            enviar_telegram_chat(c_id, f"⚪ *Confirmado:* {det}")

                        elif cb_data.startswith("desc_on:"):
                            sku = cb_data.split("desc_on:")[1]
                            ok, det = conmutar_descuento_producto(sku, activar=True)
                            responder_callback(cb_id, f"Descuento activado para {sku}")
                            enviar_telegram_chat(c_id, f"✅ *Descuento aprobado:* {det}")

                        elif cb_data.startswith("desc_off:"):
                            sku = cb_data.split("desc_off:")[1]
                            ok, det = conmutar_descuento_producto(sku, activar=False)
                            responder_callback(cb_id, f"Descuento revertido para {sku}")
                            enviar_telegram_chat(c_id, f"🔄 *Descuento revertido:* {det}")

                        elif cb_data.startswith("pedir:"):
                            sku = cb_data.split("pedir:")[1]
                            responder_callback(cb_id, f"Ordenando {sku}...")
                            conf = procesar_confirmacion_pedido(sku_objetivo=sku, origen="Telegram (Botón)")
                            enviar_telegram_chat(c_id, conf)
                        continue

                    # 2. Comandos de texto
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    texto_raw = msg.get("text", "").strip()
                    texto = texto_raw.lower().split("@")[0]

                    if not texto:
                        continue

                    if texto in ["/start", "/ayuda", "ayuda"]:
                        txt = (
                            "👋 *Centro de Control — Glazed Co.*\n\n"
                            "Comandos disponibles:\n"
                            "📉 `/novendidos` — Muestra los **5 productos de menor venta** con botones de decisión\n"
                            "🏷️ `/ofertas` — Ver sugerencias generales de descuento\n"
                            "📋 `/stock` — Consultar inventario desde Supabase\n"
                            "🏢 `/almacen` — Capacidad física y espacio libre\n"
                            "📈 `/probabilidades` — Análisis semanal con Gemini\n"
                            "🛒 `/confirmarpedido` — Orden automática del producto crítico\n"
                            "📝 `/confirmar [SKU] [CANT]` — Orden específica de compra\n"
                            "⚖️ `/stand` o `/estado` — Estado del anaquel y sensores"
                        )
                        enviar_telegram_chat(chat_id, txt)

                    elif texto in ["/novendidos", "novendidos", "/no_vendidos"]:
                        enviar_5_no_vendidos_telegram(chat_id)

                    elif texto in ["/ofertas", "/descuentos", "ofertas"]:
                        enviar_ofertas_telegram(chat_id)

                    elif texto in ["/almacen", "almacen", "/capacidad"]:
                        total_unidades, espacio_libre, pct = calcular_ocupacion_almacen()
                        barra = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                        txt = (
                            f"🏢 *ESTADO GENERAL DEL ALMACÉN*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📦 *Stock Total Actual:* `{total_unidades} uds`\n"
                            f"🚧 *Capacidad Máxima:* `{LIMITE_ALMACEN_TOTAL} uds`\n"
                            f"🟢 *Espacio Libre:* `{espacio_libre} uds disponibles`\n"
                            f"📊 *Nivel de Ocupación:* `{pct:.1f}%`\n"
                            f"`[{barra}]`\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"Usa `/stock` para el desglose por producto."
                        )
                        enviar_telegram_chat(chat_id, txt)

                    elif texto in ["/stock", "/inventario", "stock", "inventario"]:
                        enviar_telegram_chat(chat_id, reporte_stock_telegram())

                    elif texto in ["/probabilidades", "/pedidos", "/compras", "pedidos"]:
                        enviar_telegram_chat(chat_id, "⏳ _Consultando Supabase y analizando con Gemini Flash..._")
                        analisis = analizar_probabilidad_semanal_gemini()
                        enviar_telegram_chat(chat_id, f"📊 *ANÁLISIS PROBABILÍSTICO DE COMPRAS:*\n\n{analisis}")

                    elif texto in ["/confirmarpedido", "confirmarpedido", "/reordenar"]:
                        enviar_telegram_chat(chat_id, "⚙️ _Calculando pedido óptimo con control de almacén..._")
                        conf = procesar_confirmacion_pedido(origen="Telegram")
                        enviar_telegram_chat(chat_id, conf)

                    elif texto.startswith("/confirmar") or texto.startswith("confirmar"):
                        partes = texto_raw.split()
                        sku_input = partes[1] if len(partes) > 1 else None
                        cant_input = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else None
                        conf = procesar_confirmacion_pedido(sku_input, cant_input, origen="Telegram")
                        enviar_telegram_chat(chat_id, conf)

                    elif texto in ["/stand", "/estado", "estado"]:
                        with estado_lock:
                            info = (
                                f"🏪 *ESTADO EN VIVO DEL STAND*\n"
                                f"📦 Stock Anaquel: `{estado['stock_anaquel']} uds`\n"
                                f"⚖️ Peso Báscula: `{estado['peso_gramos']} g`\n"
                                f"🚨 Alertas Emitidas: `{estado['total_alertas']}`\n"
                                f"⚡ Último Evento: _{estado['ultimo_evento']}_"
                            )
                        enviar_telegram_chat(chat_id, info)

            elif r.status_code == 409:
                time.sleep(4)

        except Exception:
            time.sleep(2)
        time.sleep(1)

# =======================================================
# 8. ENDPOINTS REST
# =======================================================
@app.route('/api/inventario', methods=['GET'])
def api_inventario():
    catalogo = obtener_catalogo_supabase()
    total_unidades, espacio_libre, pct = calcular_ocupacion_almacen()
    return jsonify({
        "status": "success",
        "total": len(catalogo),
        "total_unidades_almacen": total_unidades,
        "limite_almacen": LIMITE_ALMACEN_TOTAL,
        "espacio_libre": espacio_libre,
        "porcentaje_ocupacion": pct,
        "productos": catalogo
    })

@app.route('/api/confirmar-pedido', methods=['POST'])
def api_confirmar_pedido():
    data = request.get_json(force=True) if request.is_json else {}
    sku = data.get("sku")
    cantidad = data.get("cantidad")
    resultado = procesar_confirmacion_pedido(sku, cantidad, origen="Dashboard Web")
    return jsonify({"status": "success", "detalle": resultado})

@app.route('/api/esp32/salida-anaquel', methods=['POST'])
def esp32_salida():
    data = request.get_json(force=True)
    sku = str(data.get("sku", "SKU-102560"))

    catalogo = obtener_catalogo_supabase()
    for p in catalogo:
        if p["sku"].upper() == sku.upper():
            nuevo_stk = max(0, p["stock_actual"] - 1)
            actualizar_stock_seguro(sku, nuevo_stk)
            break

    with estado_lock:
        estado["stock_anaquel"] = max(0, estado["stock_anaquel"] - 1)
        estado["ultimo_evento"] = f"ESP32-CAM: Retiro ({sku})"
        nuevo_stock = estado["stock_anaquel"]

    if nuevo_stock <= 2:
        estado["total_alertas"] += 1
        alerta_msg = f"⚠️ *Glazed Co. | Desabasto Crítico*\nEl producto `{sku}` tiene solo *{nuevo_stock} unidades* en anaquel."
        threading.Thread(target=enviar_telegram_chat, args=(TELEGRAM_CHAT_ID, alerta_msg), daemon=True).start()

    return jsonify({"status": "success", "sku": sku, "stock_restante": nuevo_stock})

@app.route('/api/peso', methods=['POST'])
def recibir_peso():
    data = request.get_json(force=True)
    nuevo_stock = data.get("stock", estado["stock_anaquel"])
    peso_g = data.get("peso", estado["peso_gramos"])
    evento = data.get("evento", "actualizacion")

    with estado_lock:
        estado["stock_anaquel"] = nuevo_stock
        estado["peso_gramos"] = peso_g
        estado["ultimo_evento"] = f"Báscula: {evento} ({nuevo_stock} uds, {peso_g}g)"

    return jsonify({"status": "success", "stock_actual": nuevo_stock})

@app.route('/api/analisis-gemini', methods=['GET'])
def api_analisis_gemini():
    resultado = analizar_probabilidad_semanal_gemini()
    with estado_lock:
        estado["recomendacion_ia"] = resultado
    return jsonify({"analisis": resultado})

@app.route('/api/estado')
def get_estado():
    with estado_lock:
        return jsonify(estado)

# =======================================================
# 9. DASHBOARD WEB (SIN PRECIO OFERTA NI BOTONES APROBAR)
# =======================================================
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>DAI !</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen p-4 md:p-8 font-sans">
  <div class="max-w-7xl mx-auto space-y-6">

    <header class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 pb-6 border-b border-slate-800">
      <div class="flex items-center gap-3">
        <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-2xl text-emerald-400 text-2xl">
          <i class="fa-solid fa-mug-saucer"></i>
        </div>
        <div>
          <h1 class="text-2xl font-bold tracking-tight text-white">DAI !</h1>
          <p class="text-xs text-slate-400">Tabla <code>products</code> en Supabase, Báscula IoT & Predicción Semanal con Gemini Flash</p>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="px-3 py-1 text-xs rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800 flex items-center gap-1.5 font-medium">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Bascula Conectada
        </span>
        <span class="px-3 py-1 text-xs rounded-full bg-sky-950 text-sky-400 border border-sky-800 font-medium">
          <i class="fa-solid fa-brain"></i> IA de analisis Activada
        </span>
        <span class="px-3 py-1 text-xs rounded-full bg-teal-950 text-teal-400 border border-teal-800 font-medium">
          <i class="fa-solid fa-database"></i> Base de datos conectada
        </span>
      </div>
    </header>

    <!-- KPIS -->
    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex items-center justify-between">
        <div>
          <p class="text-xs font-semibold text-slate-400 uppercase">Stock en Anaquel</p>
          <p id="kpi-stock" class="text-3xl font-extrabold text-emerald-400 mt-1">--</p>
          <span id="kpi-peso" class="text-[11px] text-slate-400">0.0 g registrados</span>
        </div>
        <div class="p-3 bg-emerald-500/10 text-emerald-400 rounded-xl text-2xl"><i class="fa-solid fa-box-archive"></i></div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex items-center justify-between">
        <div>
          <p class="text-xs font-semibold text-slate-400 uppercase">Capacidad Almacén</p>
          <p id="kpi-almacen" class="text-2xl font-extrabold text-sky-400 mt-1">15 / 300</p>
          <span id="kpi-almacen-pct" class="text-[11px] text-slate-400">15</span>
        </div>
        <div class="p-3 bg-sky-500/10 text-sky-400 rounded-xl text-2xl"><i class="fa-solid fa-warehouse"></i></div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex items-center justify-between">
        <div>
          <p class="text-xs font-semibold text-slate-400 uppercase">Catalogo </p>
          <p id="kpi-total-prods" class="text-3xl font-extrabold text-teal-400 mt-1">15</p>
          <span class="text-[11px] text-teal-400">En tabla products</span>
        </div>
        <div class="p-3 bg-teal-500/10 text-teal-400 rounded-xl text-2xl"><i class="fa-solid fa-cloud-arrow-up"></i></div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex items-center justify-between">
        <div>
          <p class="text-xs font-semibold text-slate-400 uppercase">Último Evento</p>
          <p id="kpi-evento" class="text-xs font-bold text-slate-200 mt-2 truncate max-w-[150px]">En espera...</p>
          <span class="text-[11px] text-emerald-400"><i class="fa-solid fa-bolt"></i> Sensor Activo</span>
        </div>
        <div class="p-3 bg-violet-500/10 text-violet-400 rounded-xl text-2xl"><i class="fa-solid fa-clock-rotate-left"></i></div>
      </div>
    </section>

    <!-- PANEL PRINCIPAL -->
    <main class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="lg:col-span-2 bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col justify-between">
        <div>
          <div class="flex justify-between items-center pb-4 mb-4 border-b border-slate-800">
            <div class="flex items-center gap-2 text-sky-400 font-bold text-sm tracking-wide uppercase">
              <i class="fa-solid fa-chart-line text-lg"></i> Probabilidad Semanal de Reorden & Prevención de Merma
            </div>
            <button onclick="solicitarAnalisisGemini()" class="px-3 py-1.5 bg-sky-500/20 hover:bg-sky-500/30 text-sky-300 border border-sky-500/40 rounded-xl text-xs font-semibold transition flex items-center gap-1.5">
              <i class="fa-solid fa-wand-magic-sparkles"></i> Evaluar 
            </button>
          </div>
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800/80 min-h-[160px] flex items-center">
            <p id="texto-ia" class="text-slate-300 text-sm leading-relaxed whitespace-pre-line italic">
              Presiona "Evaluar con Gemini" o solicita /probabilidades por Telegram para proyectar el riesgo de compra semanal...
            </p>
          </div>
        </div>
        <div class="flex items-center justify-between text-[11px] text-slate-500 mt-4 pt-3 border-t border-slate-800/50">
          <span><i class="fa-solid fa-calendar-check text-emerald-400 mr-1"></i> 
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col justify-between">
        <div>
          <h3 class="text-xs uppercase font-bold text-slate-400 tracking-wider mb-2">Comportamiento en Anaquel</h3>
          <p class="text-xs text-slate-500 mb-4">Registro continuo de la celda de carga.</p>
          <div class="h-[150px]">
            <canvas id="graficaInventario"></canvas>
          </div>
        </div>
        <div class="mt-4 pt-3 border-t border-slate-800/50 flex flex-col gap-2">
          <button onclick="confirmarPedidoWeb()" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white py-2 rounded-xl text-xs font-semibold transition flex items-center justify-center gap-2">
            <i class="fa-solid fa-cart-flatbed"></i> Confirmar Pedido Critico
          </button>
          <button onclick="simularRetiroESP32()" class="w-full bg-slate-800 hover:bg-slate-700 text-emerald-400 border border-emerald-500/30 py-2 rounded-xl text-xs font-semibold transition">
            <i class="fa-solid fa-hand-holding-box mr-1"></i> Simular 
          </button>
        </div>
      </div>
    </main>

    <!-- TABLA DE INVENTARIO CENTRAL EN SUPABASE -->
    <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
      <div class="flex justify-between items-center mb-4 pb-3 border-b border-slate-800">
        <div>
          <h3 class="text-sm uppercase font-bold text-slate-200 tracking-wider flex items-center gap-2">
            <i class="fa-solid fa-boxes-stacked text-emerald-400"></i> Tabla Productos (En Vivo)
          </h3>
          <p class="text-xs text-slate-500">Sincronización en tiempo real entre Supabase, Dashboard y Telegram.</p>
        </div>
        <button onclick="cargarInventario()" class="px-3.5 py-1.5 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 rounded-xl text-xs font-semibold transition">
          <i class="fa-solid fa-rotate-right mr-1"></i> Actualizar
        </button>
      </div>    

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs text-slate-300">
          <thead class="bg-slate-950 text-slate-400 uppercase border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">SKU</th>
              <th class="py-3 px-4">Producto</th>
              <th class="py-3 px-4">Ventas Sem.</th>
              <th class="py-3 px-4">Existencia</th>
              <th class="py-3 px-4">Precio Regular</th>
              <th class="py-3 px-4">Lead Time</th>
              <th class="py-3 px-4">Case Size</th>
              <th class="py-3 px-4">Acción</th>
            </tr>
          </thead>
          <tbody id="tabla-inventario" class="divide-y border-slate-800/60">
            <tr><td colspan="8" class="py-6 text-center text-slate-500">Cargando productos...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

  </div>

  <script>
    const ctx = document.getElementById('graficaInventario').getContext('2d');
    const grafica = new Chart(ctx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [{
          label: 'Piezas',
          data: [],
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.15)',
          borderWidth: 2,
          tension: 0.3,
          fill: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { beginAtZero: true, suggestedMax: 10, grid: { color: '#1e293b' }, ticks: { color: '#94a3b8' } },
          x: { grid: { color: '#1e293b' }, ticks: { color: '#94a3b8', maxTicksLimit: 5 } }
        },
        plugins: { legend: { display: false } }
      }
    });

    async function actualizarTelemetria() {
      try {
        const res = await fetch('/api/estado');
        const data = await res.json();

        document.getElementById('kpi-stock').innerText = (data.stock_anaquel ?? 0) + " pzas";
        document.getElementById('kpi-peso').innerText = (data.peso_gramos ?? 0).toFixed(1) + " g registrados";
        document.getElementById('kpi-evento').innerText = data.ultimo_evento ?? "Sin datos";

        const tiempo = new Date().toLocaleTimeString();
        if (grafica.data.labels.length > 7) {
          grafica.data.labels.shift();
          grafica.data.datasets[0].data.shift();
        }
        grafica.data.labels.push(tiempo);
        grafica.data.datasets[0].data.push(data.stock_anaquel ?? 0);
        grafica.update();
      } catch (e) {}
    }

    async function cargarInventario() {
      try {
        const res = await fetch('/api/inventario');
        const data = await res.json();
        const tbody = document.getElementById('tabla-inventario');
        tbody.innerHTML = '';
        
        document.getElementById('kpi-total-prods').innerText = data.total ?? 0;
        document.getElementById('kpi-almacen').innerText = `${data.total_unidades_almacen ?? 0} / ${data.limite_almacen ?? 300} uds`;
        document.getElementById('kpi-almacen-pct').innerText = `${(data.porcentaje_ocupacion ?? 0).toFixed(1)}% ocupado (${data.espacio_libre ?? 0} libres)`;

        data.productos.slice(0, 15).forEach(p => {
          const stk = p.stock_actual ?? 0;
          let precioNorm = `$${p.precio.toFixed(2)}`;

          let botonAccion = `<button onclick="confirmarPedidoEspecifico('${p.sku}')" class="px-2 py-1 rounded text-[10px] bg-slate-800 hover:bg-slate-700 text-sky-300 border border-sky-500/30">Reordenar</button>`;

          const fila = `
            <tr class="hover:bg-slate-800/40">
              <td class="py-3 px-4 font-mono text-emerald-400 font-semibold">${p.sku}</td>
              <td class="py-3 px-4 font-medium text-slate-200">${p.nombre}</td>
              <td class="py-3 px-4 text-slate-400">${p.ventas_semana} uds</td>
              <td class="py-3 px-4 font-bold ${stk <= 2 ? 'text-red-400 animate-pulse' : (stk <= 5 ? 'text-amber-400' : 'text-slate-100')}">${stk} uds</td>
              <td class="py-3 px-4 text-slate-300">${precioNorm}</td>
              <td class="py-3 px-4 text-slate-400">${p.lead_time_dias} d</td>
              <td class="py-3 px-4 text-slate-400">${p.case_size}</td>
              <td class="py-3 px-4">${botonAccion}</td>
            </tr>
          `;
          tbody.innerHTML += fila;
        });
      } catch (e) {}
    }

    async function solicitarAnalisisGemini() {
      document.getElementById('texto-ia').innerText = "Analizando demanda semanal, rotación y lead times en Supabase con Gemini...";
      const res = await fetch('/api/analisis-gemini');
      const data = await res.json();
      document.getElementById('texto-ia').innerText = data.analisis;
    }

    async function confirmarPedidoWeb() {
      const res = await fetch('/api/confirmar-pedido', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})
      });
      const data = await res.json();
      alert("Orden Procesada:\\n\\n" + data.detalle);
      cargarInventario();
    }

    async function confirmarPedidoEspecifico(sku) {
      const res = await fetch('/api/confirmar-pedido', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sku: sku})
      });
      const data = await res.json();
      alert("Orden Procesada:\\n\\n" + data.detalle);
      cargarInventario();
    }

    async function simularRetiroESP32() {
      await fetch('/api/esp32/salida-anaquel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sku: 'SKU-102560'})
      });
      cargarInventario();
    }

    cargarInventario();
    setInterval(actualizarTelemetria, 1500);
  </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_DASHBOARD)

# =======================================================
# 10. EJECUCIÓN PRINCIPAL
# =======================================================
if __name__ == '__main__':
    threading.Thread(target=bucle_escucha_telegram, daemon=True).start()
    print("[Flask] Servidor iniciado en http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)