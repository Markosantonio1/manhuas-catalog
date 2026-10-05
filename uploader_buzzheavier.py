# uploader_buzzheavier.py
# PLAN B: solo sube a BuzzHeavier y guarda el ID del archivo en el catálogo.
# El script local (resolver_enlaces.py) se encargará de obtener los enlaces directos.

import asyncio
import json
import os
import re
import sys
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
CATALOG_OUTPUT_FILE = BASE_DIR / "catalog_con_ids.json"   # ← nombre nuevo (Plan B)
TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_bh_uploads"

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STR = os.getenv("TELEGRAM_SESSION_STR", "").strip()

BUZZHEAVIER_ACCOUNT_ID = os.getenv("BUZZHEAVIER_ACCOUNT_ID", "").strip()

NOTIFY_USERNAME = "@Markosantonio"
MAIN_CHANNEL = "manhuasgratis"

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))

UPLOAD_TIMEOUT = 900


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
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def save_catalog_output(catalog: dict) -> None:
    CATALOG_OUTPUT_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ Catálogo con IDs guardado en: {CATALOG_OUTPUT_FILE}")


def find_packages_without_url(catalog: dict) -> list:
    """Paquetes sin download_url Y sin buzzheavier_id (aún no procesados)."""
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
# SUBIDA A BUZZHEAVIER (solo devuelve el ID)
# ============================================================

def upload_to_buzzheavier(local_path: Path) -> str | None:
    """
    Sube el archivo a BuzzHeavier.
    Devuelve SOLO el ID del archivo subido.
    NO intenta resolver el enlace directo (eso lo hará el script local).
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
        print(f"    ↑ Subiendo a BuzzHeavier: {filename} ({size_mb:.1f} MB)")

        with open(local_path, "rb") as file_handle:
            response = session.put(
                upload_url,
                data=file_handle,
                timeout=UPLOAD_TIMEOUT,
            )

        print(f"    → Respuesta: HTTP {response.status_code}")

        if response.status_code not in (200, 201):
            print(
                f"    ✗ Error HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
            return None

        # ----------------------------------------------------
        # Parsear respuesta anidada
        # { "code": 201, "data": { "id": "abc123", ... } }
        # ----------------------------------------------------
        try:
            response_data = response.json()
            inner = response_data

            if (
                isinstance(response_data, dict)
                and isinstance(response_data.get("data"), dict)
            ):
                inner = response_data["data"]

            file_id = inner.get("id") if isinstance(inner, dict) else None

            if not file_id and isinstance(response_data, dict):
                file_id = response_data.get("id")

            if file_id:
                print(f"    ✓ ID obtenido: {file_id}")
                return str(file_id)

            print(f"    ✗ No se encontró 'id' en: {response_data}")
            return None

        except json.JSONDecodeError:
            print(f"    ✗ Respuesta no es JSON: {response.text[:200]}")
            return None

    except Exception as exc:
        print(f"    ✗ Error subiendo: {type(exc).__name__}: {exc}")
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
    print(f"Paquetes sin subir detectados: {total}")

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

        # ----------------------------------------------------
        # Descargar PDF desde Telegram
        # ----------------------------------------------------
        try:
            print("    ↓ Descargando de Telegram...")

            if not message_id:
                raise ValueError("El paquete no tiene message_id.")

            message = await client.get_messages(
                MAIN_CHANNEL,
                ids=message_id,
            )

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

        # ----------------------------------------------------
        # Subir a BuzzHeavier → guardar ID en el paquete
        # ----------------------------------------------------
        buzz_id = upload_to_buzzheavier(local_file)

        if buzz_id:
            pack["buzzheavier_id"] = buzz_id
            # NO tocamos download_url (queda como estaba)
            series_stats[series_id]["success"] += 1
            success_count += 1
            print(f"    ✓ ID GUARDADO EN EL PAQUETE: {buzz_id}")
        else:
            series_stats[series_id]["fail"] += 1
            fail_count += 1
            print("    ✗ FALLÓ LA SUBIDA")

        # ----------------------------------------------------
        # Borrar archivo temporal
        # ----------------------------------------------------
        try:
            if local_file.exists():
                local_file.unlink()
        except OSError:
            pass

    # --------------------------------------------------------
    # Guardar catálogo con los IDs
    # --------------------------------------------------------
    save_catalog_output(catalog)

    remaining = total - len(to_process)

    report_lines = [
        "📊 *Uploader BuzzHeavier — Reporte (Plan B)*",
        f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"✅ Subidos: {success_count}",
        f"❌ Fallidos: {fail_count}",
        f"📦 Procesados: {len(to_process)} de {total}",
    ]

    if remaining > 0:
        report_lines.append(f"⏳ Pendientes: {remaining}")

    report_lines.append("")
    report_lines.append("*Detalle por serie:*")

    for _sid, stats in sorted(
        series_stats.items(),
        key=lambda x: x[1]["name"],
    ):
        vis_tag = "" if stats.get("visible", True) else " (oculta)"
        line = f"• {stats['name']}{vis_tag}: {stats['success']} ✓"
        if stats["fail"] > 0:
            line += f", {stats['fail']} ✗"
        report_lines.append(line)

    report_lines.append("")
    report_lines.append(
        "➡ Descarga el archivo adjunto y ejecuta "
        "`resolver_enlaces.py` en tu PC."
    )

    report = "\n".join(report_lines)

    # --------------------------------------------------------
    # Enviar reporte + archivo a Telegram
    # --------------------------------------------------------
    try:
        print(f"\nEnviando reporte a {NOTIFY_USERNAME}...")
        await client.send_message(NOTIFY_USERNAME, report, parse_mode="md")
        print("✓ Reporte enviado.")

        await client.send_file(
            NOTIFY_USERNAME,
            str(CATALOG_OUTPUT_FILE),
            caption=(
                "📎 Catálogo con IDs de BuzzHeavier.\n"
                "Descárgalo, renómbralo a `catalog_con_ids.json` "
                "y ejecuta `resolver_enlaces.py` en tu PC."
            ),
        )
        print("✓ Catálogo enviado.")

    except Exception as exc:
        print(f"⚠ Error al enviar por Telegram: {exc}")

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
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}")
        sys.exit(1)
