import cv2
import threading
import requests
from datetime import datetime
from ultralytics import YOLO
from supabase import create_client, Client

# ==========================================================
# 1. CREDENCIALES
# ==========================================================
# Supabase
SUPABASE_URL = "https://wwswaqbpookqidqkbknf.supabase.co"
SUPABASE_KEY = "sb_secret_vYT0rSqyIfu2QufzCG-iKw_mCGyVd_l"
TABLA_CONTEO = "person_counts"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Telegram
TELEGRAM_BOT_TOKEN = "8906055800:AAHvhYu4e8v2qCy9dOIt51MSKmK_Ku4mewE"
TELEGRAM_CHAT_ID = "8749748612"

# ==========================================================
# 2. FUNCIONES DE REGISTRO Y NOTIFICACIÓN (BACKGROUND THREADS)
# ==========================================================
def guardar_en_supabase(person_id: int, total_count: int):
    """Guarda el registro en Supabase en un hilo secundario."""
    def tarea():
        try:
            registro = {
                "person_id": int(person_id),
                "total_count": int(total_count),
                "created_at": datetime.utcnow().isoformat()
            }
            supabase.table(TABLA_CONTEO).insert(registro).execute()
            print(f"[Supabase] Persona #{person_id} registrada | Acumulado: {total_count}")
        except Exception as error:
            print(f"[Error Supabase]: {error}")

    threading.Thread(target=tarea, daemon=True).start()


def notificar_telegram(person_id: int, total_count: int, en_vivo: int):
    """Envía un mensaje a Telegram en segundo plano."""
    def tarea():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            mensaje = (
                f"🚨 *Nueva persona detectada*\n"
                f"👤 *ID Trackeado:* #{person_id}\n"
                f"👥 *Personas en vivo:* {en_vivo}\n"
                f"📈 *Total acumulado:* {total_count}\n"
                f"🕒 *Hora:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensaje,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=5)
            print(f"[Telegram] Notificación enviada para ID #{person_id}")
        except Exception as error:
            print(f"[Error Telegram]: {error}")

    threading.Thread(target=tarea, daemon=True).start()

# ==========================================================
# 3. INICIALIZACIÓN DE YOLO
# ==========================================================
modelo = YOLO("yolov8n.pt")
CLASE_PERSONA = 0

personas_registradas = set()
contador_acumulado = 0

# ==========================================================
# 4. CAPTURA DE WEBCAM
# ==========================================================
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

if not cap.isOpened():
    print("[ERROR] No se pudo acceder a la webcam.")
    exit()

print("[INFO] Sistema de detección activo. Presiona 'q' para salir.")

# ==========================================================
# 5. BUCLE PRINCIPAL DE DETECCIÓN
# ==========================================================
while True:
    ret, frame = cap.read()
    if not ret:
        print("[ERROR] No se pudo leer el fotograma.")
        break

    resultados = modelo.track(
        frame, 
        persist=True, 
        classes=[CLASE_PERSONA], 
        tracker="bytetrack.yaml", 
        verbose=False
    )

    personas_en_vivo = 0

    if resultados[0].boxes and resultados[0].boxes.id is not None:
        cajas = resultados[0].boxes.xyxy.cpu().numpy()
        ids = resultados[0].boxes.id.int().cpu().tolist()
        personas_en_vivo = len(ids)

        for caja, track_id in zip(cajas, ids):
            x1, y1, x2, y2 = map(int, caja)

            # Persona nueva no vista anteriormente
            if track_id not in personas_registradas:
                personas_registradas.add(track_id)
                contador_acumulado += 1

                # 1. Enviar a base de datos
                guardar_en_supabase(person_id=track_id, total_count=contador_acumulado)

                # 2. Enviar alerta a Telegram
                notificar_telegram(
                    person_id=track_id, 
                    total_count=contador_acumulado, 
                    en_vivo=personas_en_vivo
                )

            # Dibujar recuadro y etiqueta
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                frame,
                f"Persona #{track_id}",
                (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2
            )

    # Marcadores en pantalla
    cv2.rectangle(frame, (10, 10), (320, 85), (20, 20, 20), -1)
    cv2.putText(
        frame,
        f"Total que han pasado: {contador_acumulado}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),
        2
    )
    cv2.putText(
        frame,
        f"En vivo ahora: {personas_en_vivo}",
        (20, 70),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0),
        2
    )

    cv2.imshow("Detector y Contador Multiple - YOLO + Supabase + Telegram", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()