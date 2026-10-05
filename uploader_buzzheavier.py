# uploader_buzzheavier.py
import asyncio
import json
import os
import re
import sys
import subprocess
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
CATALOG_OUTPUT_FILE = BASE_DIR / "catalog_con_urls.json"
TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_bh_uploads"

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STR = os.getenv("TELEGRAM_SESSION_STR", "").strip()

NOTIFY_USERNAME = "@Markosantonio"
MAIN_CHANNEL = "manhuasgratis"

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))


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
        raise FileNotFoundError(f"No se encontró el catálogo: {CATALOG_FILE}")
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def save_catalog_output(catalog: dict) -> None:
    CATALOG_OUTPUT_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"✓ Catálogo actualizado guardado en: {CATALOG_OUTPUT_FILE}")


def find_packages_without_url(catalog: dict) -> list:
    pending = []
    for list_key in ("series", "novels"):
        for series in catalog.get(list_key, []):
            for pack in series.get("packages", []):
                url = pack.get("download_url")
                if not url or not str(url).strip():
                    pending.append((series, pack))
    return pending


# ============================================================
# SUBIDA A BUZZHEAVIER (obtener ID) Y ENLACE DIRECTO (con CLI)
# ============================================================
def upload_to_buzzheavier(local_path: Path) -> str | None:
    """
    Sube el archivo a BuzzHeavier y devuelve el ID del archivo
    (el que se usa luego con bhdownloader).
    """
    filename = local_path.name
    encoded_name = quote(filename)
    url = f"https://w.buzzheavier.com/{encoded_name}"

    headers = {"User-Agent": "Mozilla/5.0 (Android) ManhuasApp/1.0"}

    session = requests.Session()
    session.headers.update(headers)

    try:
        size_mb = local_path.stat().st_size / (1024 * 1024)
        print(f"    ↑ Subiendo a BuzzHeavier: {filename} ({size_mb:.1f} MB)")

        with open(local_path, "rb") as f:
            response = session.put(url, data=f, timeout=900)

        if response.status_code not in (200, 201):
            print(f"    ✗ Error HTTP {response.status_code}: {response.text[:300]}")
            return None

        response_data = response.json()
        # La respuesta tiene la forma: {"code": 201, "data": {"id": "..."}}
        inner = response_data.get("data", response_data)
        file_id = inner.get("id")

        if file_id:
            print(f"    ✓ Archivo subido. ID: {file_id}")
            return file_id
        else:
            print(f"    ✗ No se encontró el ID en la respuesta: {response_data}")
            return None

    except Exception as exc:
        print(f"    ✗ Error subiendo: {type(exc).__name__}: {exc}")
        return None
    finally:
        session.close()


def get_direct_link(file_id: str) -> str | None:
    """
    Ejecuta 'bhdownloader -l <file_id>' y devuelve la URL directa.
    """
    try:
        result = subprocess.run(
            ["bhdownloader", "-l", file_id],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            url = lines[-1].strip()
            if url.startswith("http"):
                return url
        return None
    except subprocess.CalledProcessError as exc:
        print(f"    ⚠ bhdownloader falló: {exc}")
        return None
    except FileNotFoundError:
        print("    ⚠ Comando 'bhdownloader' no encontrado. ¿Se instaló?")
        return None
    except Exception as exc:
        print(f"    ⚠ Error ejecutando bhdownloader: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# PROCESO PRINCIPAL
# ============================================================
async def main():
    print("========================================")
    print("UPLOADER BUZZHEAVIER — GitHub Actions")
    print("========================================")

    if not TELEGRAM_SESSION_STR:
        raise RuntimeError("Falta TELEGRAM_SESSION_STR en las variables de entorno.")

    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION_STR),
        API_ID,
        API_HASH,
    )

    print("Conectando con Telegram...")
    await client.start()
    if not await client.is_user_authorized():
        raise RuntimeError("La sesión de Telegram no está autorizada.")
    me = await client.get_me()
    print(f"✓ Conectado como: {me.first_name} (@{me.username})")

    catalog = load_catalog()
    pending = find_packages_without_url(catalog)
    total = len(pending)
    print(f"Paquetes sin URL detectados: {total}")

    if total == 0:
        print("Nada que procesar.")
        await client.disconnect()
        return

    to_process = pending[:MAX_PACKAGES_PER_RUN]
    print(f"Procesando {len(to_process)} paquetes (límite: {MAX_PACKAGES_PER_RUN})")

    success_count = 0
    fail_count = 0
    series_stats: dict[str, dict] = {}

    for idx, (series, pack) in enumerate(to_process, start=1):
        series_name = series.get("name", "Desconocida")
        series_id = series.get("id", "")
        pack_name = pack.get("name", "Paquete")
        message_id = pack.get("message_id")
        filename = pack.get("filename", f"{message_id}.pdf")
        is_visible = series.get("visible", True)
        tag = "" if is_visible else " [OCULTA]"

        print(f"\n[{idx}/{len(to_process)}] {series_name}{tag} → {pack_name}")

        if series_id not in series_stats:
            series_stats[series_id] = {
                "name": series_name,
                "success": 0,
                "fail": 0,
                "visible": is_visible,
            }

        local_file = TEMP_DOWNLOAD_DIR / sanitize_filename(filename)

        try:
            print(f"    ↓ Descargando de Telegram...")
            message = await client.get_messages(MAIN_CHANNEL, ids=message_id)
            if not message or not message.file:
                raise ValueError(f"Mensaje {message_id} sin archivo.")
            await client.download_media(message, file=str(local_file))
            if not local_file.exists() or local_file.stat().st_size == 0:
                raise ValueError("Archivo vacío.")
            size_mb = local_file.stat().st_size / (1024 * 1024)
            print(f"    ✓ Descargado: {size_mb:.1f} MB")
        except Exception as exc:
            print(f"    ✗ Error descargando: {exc}")
            series_stats[series_id]["fail"] += 1
            fail_count += 1
            if local_file.exists():
                try:
                    local_file.unlink()
                except OSError:
                    pass
            continue

        # 1. Subir el archivo a BuzzHeavier y obtener el ID
        file_id = upload_to_buzzheavier(local_file)
        if not file_id:
            series_stats[series_id]["fail"] += 1
            fail_count += 1
            if local_file.exists():
                try: local_file.unlink()
                except OSError: pass
            continue

        # 2. Usar el ID con bhdownloader para obtener el enlace directo
        print(f"    → Extrayendo enlace directo con bhdownloader...")
        download_url = get_direct_link(file_id)

        if download_url:
            pack["download_url"] = download_url
            series_stats[series_id]["success"] += 1
            success_count += 1
        else:
            series_stats[series_id]["fail"] += 1
            fail_count += 1

        try:
            if local_file.exists():
                local_file.unlink()
        except OSError:
            pass

    save_catalog_output(catalog)

    remaining = total - len(to_process)
    report_lines = [
        "📊 *Uploader BuzzHeavier — Reporte*",
        f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"✅ Subidos: {success_count}",
        f"❌ Fallidos: {fail_count}",
        f"📦 Procesados: {len(to_process)} de {total}",
    ]
    if remaining > 0:
        report_lines.append(f"⏳ Pendientes para la próxima ejecución: {remaining}")

    report_lines.append("")
    report_lines.append("*Detalle por serie:*")
    for sid, stats in sorted(series_stats.items(), key=lambda x: x[1]["name"]):
        vis_tag = "" if stats.get("visible", True) else " (oculta)"
        line = f"• {stats['name']}{vis_tag}: {stats['success']} ✓"
        if stats["fail"] > 0:
            line += f", {stats['fail']} ✗"
        report_lines.append(line)

    report = "\n".join(report_lines)

    try:
        print(f"\nEnviando reporte a {NOTIFY_USERNAME}...")
        await client.send_message(NOTIFY_USERNAME, report, parse_mode="md")
        print("✓ Reporte enviado.")

        await client.send_file(
            NOTIFY_USERNAME,
            str(CATALOG_OUTPUT_FILE),
            caption="📎 Catálogo con URLs actualizadas."
        )
        print("✓ Catálogo enviado.")
    except Exception as exc:
        print(f"⚠ Error al enviar: {exc}")

    await client.disconnect()
    print("\n========================================")
    print("PROCESO COMPLETADO")
    print("========================================")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelado.")
    except Exception as exc:
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}")
        sys.exit(1)
