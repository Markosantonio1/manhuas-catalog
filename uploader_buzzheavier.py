# uploader_buzzheavier.py
# PLAN B: sube a BuzzHeavier y guarda el ID del archivo en el catálogo.
# El resolver_enlaces.py se encarga posteriormente de obtener los enlaces directos.

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CATALOG_FILE = BASE_DIR / "catalog.json"
CATALOG_OUTPUT_FILE = BASE_DIR / "catalog_con_ids.json"
TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_bh_uploads"

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STR = os.getenv("TELEGRAM_SESSION_STR", "").strip()

BUZZHEAVIER_ACCOUNT_ID = os.getenv("BUZZHEAVIER_ACCOUNT_ID", "").strip()

NOTIFY_USERNAME = "@Markosantonio"
MAIN_CHANNEL = "manhuasgratis"

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))

# Reintentos del descargado desde Telegram.
DOWNLOAD_ATTEMPTS = int(os.getenv("DOWNLOAD_ATTEMPTS", "5"))
DOWNLOAD_RETRY_DELAY = float(os.getenv("DOWNLOAD_RETRY_DELAY", "5"))

# Tiempo máximo permitido para una descarga individual.
# No es un timeout de cada bloque de Telegram; limita el archivo completo.
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))

# Parámetros de resiliencia de Telethon.
TELEGRAM_REQUEST_RETRIES = int(os.getenv("TELEGRAM_REQUEST_RETRIES", "8"))
TELEGRAM_CONNECTION_RETRIES = int(os.getenv("TELEGRAM_CONNECTION_RETRIES", "8"))
TELEGRAM_RETRY_DELAY = float(os.getenv("TELEGRAM_RETRY_DELAY", "2"))
TELEGRAM_TIMEOUT = int(os.getenv("TELEGRAM_TIMEOUT", "30"))

UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "900"))


# ============================================================
# UTILIDADES
# ============================================================

def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", value or "")
    cleaned = re.sub(r"[\x00-\x1f]", "_", cleaned)
    cleaned = cleaned.strip()
    return cleaned or "archivo.pdf"


def load_catalog() -> dict:
    if not CATALOG_FILE.exists():
        raise FileNotFoundError(
            f"No se encontró el catálogo: {CATALOG_FILE}"
        )

    return json.loads(
        CATALOG_FILE.read_text(encoding="utf-8")
    )


def save_catalog_output(catalog: dict) -> None:
    """
    Guarda de forma atómica para que un corte no deje
    catalog_con_ids.json corrupto.
    """
    temp_path = CATALOG_OUTPUT_FILE.with_suffix(".tmp.json")

    temp_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp_path.replace(CATALOG_OUTPUT_FILE)

    print(
        f"    ✓ Catálogo checkpoint guardado: {CATALOG_OUTPUT_FILE}"
    )


def find_packages_without_url(catalog: dict) -> list:
    """Paquetes sin download_url Y sin buzzheavier_id."""
    pending = []

    for list_key in ("series", "novels"):
        for series in catalog.get(list_key, []):
            for pack in series.get("packages", []):
                url = pack.get("download_url")
                buzz_id = pack.get("buzzheavier_id")

                has_url = url and str(url).strip()
                has_id = buzz_id and str(buzz_id).strip()

                if not has_url and not has_id:
                    pending.append((series, pack))

    return pending


# ============================================================
# TELEGRAM - CLIENTE ROBUSTO
# ============================================================

async def create_telegram_client() -> TelegramClient:
    """
    Crea y conecta un cliente nuevo.

    Crear un cliente nuevo después de una desconexión es más seguro
    que intentar reutilizar un sender interno que pudo quedar en mal
    estado durante una descarga.
    """
    client = TelegramClient(
        StringSession(TELEGRAM_SESSION_STR),
        API_ID,
        API_HASH,
        timeout=TELEGRAM_TIMEOUT,
        request_retries=TELEGRAM_REQUEST_RETRIES,
        connection_retries=TELEGRAM_CONNECTION_RETRIES,
        retry_delay=TELEGRAM_RETRY_DELAY,
        auto_reconnect=True,
        flood_sleep_threshold=60,
    )

    await client.start()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("La sesión de Telegram no está autorizada.")

    return client


async def reconnect_telegram_client(client: TelegramClient) -> TelegramClient:
    """Cierra el cliente actual y levanta uno completamente nuevo."""
    print("      ↻ Reiniciando conexión de Telegram...")

    try:
        await client.disconnect()
    except BaseException as exc:
        print(
            f"      ⚠ Error cerrando cliente anterior: "
            f"{type(exc).__name__}: {exc}"
        )

    await asyncio.sleep(2)

    new_client = await create_telegram_client()

    me = await new_client.get_me()
    print(
        f"      ✓ Telegram reconectado como: "
        f"{me.first_name} (@{me.username})"
    )

    return new_client


async def download_from_telegram_with_retries(
    client: TelegramClient,
    message_id: int,
    local_file: Path,
) -> tuple[TelegramClient, bool]:
    """
    Descarga un archivo desde Telegram con varios niveles de recuperación.

    Importante: asyncio.CancelledError no hereda de Exception en Python 3.11,
    por lo que el código original no la atrapaba. Aquí se trata como fallo
    recuperable de una descarga y se reconstruye el cliente antes de reintentar.
    """

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            if local_file.exists():
                try:
                    local_file.unlink()
                except OSError:
                    pass

            print(
                f"    ↓ Descargando de Telegram "
                f"(intento {attempt}/{DOWNLOAD_ATTEMPTS})..."
            )

            if not client.is_connected():
                client = await reconnect_telegram_client(client)

            message = await client.get_messages(
                MAIN_CHANNEL,
                ids=message_id,
            )

            if not message or not message.file:
                raise ValueError(
                    f"Mensaje {message_id} sin archivo."
                )

            # Timeout del archivo completo. Si vence, se convierte en
            # TimeoutError y entra en el camino normal de reintento.
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                await client.download_media(
                    message,
                    file=str(local_file),
                )

            if (
                not local_file.exists()
                or local_file.stat().st_size == 0
            ):
                raise ValueError("Archivo vacío.")

            size_mb = local_file.stat().st_size / (1024 * 1024)
            print(f"    ✓ Descargado: {size_mb:.1f} MB")

            return client, True

        except asyncio.CancelledError as exc:
            # Python 3.11: CancelledError hereda de BaseException.
            # En este uploader la tratamos como una cancelación transitoria
            # del trabajo de descarga, no como cancelación deliberada del job.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()

            print(
                "    ⚠ Telegram canceló la tarea durante la descarga: "
                f"{exc!r}"
            )

            if attempt >= DOWNLOAD_ATTEMPTS:
                print(
                    "    ✗ Se agotaron los reintentos después de "
                    "CancelledError."
                )
                return client, False

            try:
                client = await reconnect_telegram_client(client)
            except Exception as reconnect_exc:
                print(
                    "      ⚠ No se pudo reconectar: "
                    f"{type(reconnect_exc).__name__}: {reconnect_exc}"
                )

            await asyncio.sleep(DOWNLOAD_RETRY_DELAY)

        except (TimeoutError, asyncio.TimeoutError) as exc:
            print(
                "    ⚠ Tiempo agotado descargando desde Telegram: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt >= DOWNLOAD_ATTEMPTS:
                print("    ✗ Se agotaron los reintentos por timeout.")
                return client, False

            try:
                client = await reconnect_telegram_client(client)
            except Exception as reconnect_exc:
                print(
                    "      ⚠ No se pudo reconectar: "
                    f"{type(reconnect_exc).__name__}: {reconnect_exc}"
                )

            await asyncio.sleep(DOWNLOAD_RETRY_DELAY)

        except Exception as exc:
            print(
                f"    ⚠ Error descargando: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt >= DOWNLOAD_ATTEMPTS:
                print("    ✗ Se agotaron los reintentos de descarga.")
                return client, False

            # Solo reconstruimos la conexión para errores que podrían ser
            # causados por el estado de Telegram/red. Para errores de datos
            # de negocio (por ejemplo, mensaje sin archivo), reintentar no
            # aporta, pero sigue siendo seguro y evita matar todo el lote.
            try:
                client = await reconnect_telegram_client(client)
            except Exception as reconnect_exc:
                print(
                    "      ⚠ No se pudo reconectar: "
                    f"{type(reconnect_exc).__name__}: {reconnect_exc}"
                )

            await asyncio.sleep(DOWNLOAD_RETRY_DELAY)

    return client, False


# ============================================================
# SUBIDA A BUZZHEAVIER (solo devuelve el ID)
# ============================================================

def upload_to_buzzheavier(local_path: Path) -> str | None:
    """
    Sube el archivo a BuzzHeavier.
    Devuelve SOLO el ID del archivo subido.
    NO intenta resolver el enlace directo.
    """

    filename = local_path.name
    encoded_name = quote(filename, safe="")

    upload_url = f"https://w.buzzheavier.com/{encoded_name}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Android) ManhuasApp/1.0",
    }

    if BUZZHEAVIER_ACCOUNT_ID:
        headers["Authorization"] = f"Bearer {BUZZHEAVIER_ACCOUNT_ID}"

    session = requests.Session()
    session.headers.update(headers)

    try:
        size_mb = local_path.stat().st_size / (1024 * 1024)
        print(
            f"    ↑ Subiendo a BuzzHeavier: "
            f"{filename} ({size_mb:.1f} MB)"
        )

        with open(local_path, "rb") as file_handle:
            response = session.put(
                upload_url,
                data=file_handle,
                timeout=UPLOAD_TIMEOUT,
            )

        print(
            f"    → Respuesta: HTTP {response.status_code}"
        )

        if response.status_code not in (200, 201):
            print(
                f"    ✗ Error HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
            return None

        try:
            response_data = response.json()
            inner = response_data

            if (
                isinstance(response_data, dict)
                and isinstance(response_data.get("data"), dict)
            ):
                inner = response_data["data"]

            file_id = (
                inner.get("id")
                if isinstance(inner, dict)
                else None
            )

            if not file_id and isinstance(response_data, dict):
                file_id = response_data.get("id")

            if file_id:
                print(f"    ✓ ID obtenido: {file_id}")
                return str(file_id)

            print(
                f"    ✗ No se encontró 'id' en: {response_data}"
            )
            return None

        except json.JSONDecodeError:
            print(
                f"    ✗ Respuesta no es JSON: "
                f"{response.text[:200]}"
            )
            return None

    except Exception as exc:
        print(
            f"    ✗ Error subiendo: "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    finally:
        try:
            session.close()
        except Exception:
            pass


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

async def main():
    print("========================================")
    print("UPLOADER BUZZHEAVIER — GitHub Actions")
    print("(Plan B: solo guarda el ID de cada archivo)")
    print("========================================")

    if not TELEGRAM_SESSION_STR:
        raise RuntimeError(
            "Falta TELEGRAM_SESSION_STR en las variables de entorno."
        )

    if not API_ID or not API_HASH:
        raise RuntimeError(
            "Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH."
        )

    TEMP_DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("Conectando con Telegram...")

    client = await create_telegram_client()

    me = await client.get_me()
    print(
        f"✓ Conectado como: {me.first_name} (@{me.username})"
    )

    catalog = load_catalog()
    pending = find_packages_without_url(catalog)
    total = len(pending)

    print(
        f"Paquetes sin subir detectados: {total}"
    )

    if total == 0:
        print("Nada que procesar.")
        await client.disconnect()
        return

    to_process = pending[:MAX_PACKAGES_PER_RUN]

    print(
        f"Procesando {len(to_process)} paquetes "
        f"(límite: {MAX_PACKAGES_PER_RUN})"
    )

    success_count = 0
    fail_count = 0
    series_stats: dict[str, dict] = {}

    try:
        for idx, (series, pack) in enumerate(
            to_process,
            start=1,
        ):
            series_name = series.get(
                "name",
                "Desconocida",
            )
            series_id = series.get("id", "")
            pack_name = pack.get("name", "Paquete")
            message_id = pack.get("message_id")
            filename = pack.get(
                "filename",
                f"{message_id}.pdf",
            )
            is_visible = series.get("visible", True)
            tag = "" if is_visible else " [OCULTA]"

            print(
                f"\n[{idx}/{len(to_process)}] "
                f"{series_name}{tag} → {pack_name}"
            )

            if series_id not in series_stats:
                series_stats[series_id] = {
                    "name": series_name,
                    "success": 0,
                    "fail": 0,
                    "visible": is_visible,
                }

            local_file = (
                TEMP_DOWNLOAD_DIR
                / sanitize_filename(filename)
            )

            # ------------------------------------------------
            # Descargar desde Telegram con recuperación
            # ------------------------------------------------

            if not message_id:
                print(
                    "    ✗ El paquete no tiene message_id."
                )
                series_stats[series_id]["fail"] += 1
                fail_count += 1
                continue

            client, downloaded = (
                await download_from_telegram_with_retries(
                    client,
                    int(message_id),
                    local_file,
                )
            )

            if not downloaded:
                series_stats[series_id]["fail"] += 1
                fail_count += 1

                print(
                    "    ✗ NO SE PUDO DESCARGAR EL PAQUETE; "
                    "SE CONTINÚA CON EL SIGUIENTE."
                )

                try:
                    if local_file.exists():
                        local_file.unlink()
                except OSError:
                    pass

                continue

            # ------------------------------------------------
            # Subir a BuzzHeavier
            # ------------------------------------------------

            buzz_id = upload_to_buzzheavier(local_file)

            if buzz_id:
                pack["buzzheavier_id"] = buzz_id

                series_stats[series_id]["success"] += 1
                success_count += 1

                print(
                    f"    ✓ ID GUARDADO EN EL PAQUETE: {buzz_id}"
                )

                # CHECKPOINT INMEDIATO.
                # Si el archivo siguiente falla, los IDs ya subidos
                # quedan reflejados en el catálogo de esta ejecución.
                save_catalog_output(catalog)

            else:
                series_stats[series_id]["fail"] += 1
                fail_count += 1

                print(
                    "    ✗ FALLÓ LA SUBIDA; "
                    "SE CONTINÚA CON EL SIGUIENTE."
                )

            # ------------------------------------------------
            # Borrar temporal
            # ------------------------------------------------

            try:
                if local_file.exists():
                    local_file.unlink()
            except OSError:
                pass

            # Pequeña pausa entre paquetes para no golpear Telegram
            # inmediatamente al terminar una subida.
            if idx < len(to_process):
                await asyncio.sleep(1)

    finally:
        # Siempre dejamos el último estado disponible en disco si es posible.
        try:
            save_catalog_output(catalog)
        except Exception as exc:
            print(
                f"⚠ No se pudo guardar checkpoint final: {exc}"
            )

    # --------------------------------------------------------
    # Reporte
    # --------------------------------------------------------

    remaining = total - len(to_process)

    report_lines = [
        "📊 *Uploader BuzzHeavier — Reporte (Plan B)*",
        (
            "Fecha: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
        ),
        "",
        f"✅ Subidos: {success_count}",
        f"❌ Fallidos: {fail_count}",
        f"📦 Procesados: {len(to_process)} de {total}",
    ]

    if remaining > 0:
        report_lines.append(
            f"⏳ Pendientes: {remaining}"
        )

    report_lines.append("")
    report_lines.append("*Detalle por serie:*")

    for _sid, stats in sorted(
        series_stats.items(),
        key=lambda x: x[1]["name"],
    ):
        vis_tag = (
            ""
            if stats.get("visible", True)
            else " (oculta)"
        )

        line = (
            f"• {stats['name']}{vis_tag}: "
            f"{stats['success']} ✓"
        )

        if stats["fail"] > 0:
            line += f", {stats['fail']} ✗"

        report_lines.append(line)

    report = "\n".join(report_lines)

    try:
        print(
            f"\nEnviando reporte a {NOTIFY_USERNAME}..."
        )

        await client.send_message(
            NOTIFY_USERNAME,
            report,
            parse_mode="md",
        )

        print("✓ Reporte enviado.")

        await client.send_file(
            NOTIFY_USERNAME,
            str(CATALOG_OUTPUT_FILE),
            caption=(
                "📎 Catálogo con IDs de BuzzHeavier."
            ),
        )

        print("✓ Catálogo enviado.")

    except Exception as exc:
        print(
            f"⚠ Error al enviar por Telegram: {exc}"
        )

    await client.disconnect()

    print("\n========================================")
    print("PROCESO COMPLETADO")
    print("========================================")


# ============================================================
# ARRANQUE
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Cancelado.")

    except Exception as exc:
        print(
            f"ERROR FATAL: {type(exc).__name__}: {exc}"
        )
        sys.exit(1)
