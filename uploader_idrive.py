# uploader_idrive.py
import asyncio
import gc
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CATALOG_FILE = BASE_DIR / "catalog.json"

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STR = os.getenv("TELEGRAM_SESSION_STR", "").strip()

# IDrive e2
IDRIVE_ENDPOINT = os.getenv("IDRIVE_ENDPOINT")
IDRIVE_ACCESS_KEY = os.getenv("IDRIVE_ACCESS_KEY")
IDRIVE_SECRET_KEY = os.getenv("IDRIVE_SECRET_KEY")
IDRIVE_BUCKET = os.getenv("IDRIVE_BUCKET")
IDRIVE_REGION = os.getenv("IDRIVE_REGION", "us-southeast-1")

NOTIFY_USERNAME = "@Markosantonio"
MAIN_CHANNEL = "manhuasgratis"

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))

# Cada cuántos paquetes se hace checkpoint (commit + push remoto)
SAVE_EVERY_N = int(os.getenv("SAVE_EVERY_N", "10"))

# Prefirmadas duran máximo 7 días (604800 segundos)
PRESIGN_EXPIRATION = 604800

TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_idrive_uploads"


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


def save_catalog(catalog: dict, silent: bool = False) -> None:
    CATALOG_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not silent:
        print(f"✓ Catálogo guardado")


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
# GIT — CHECKPOINTS REMOTOS
# ============================================================

def git_commit_and_push(mensaje: str) -> bool:
    """Hace git config + add + commit + push. No aborta si falla."""
    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "add", "catalog.json"],
            check=False, capture_output=True,
        )
        c = subprocess.run(
            ["git", "commit", "-m", mensaje, "--allow-empty"],
            check=False, capture_output=True, text=True,
        )
        if c.returncode != 0:
            print(f"    ⚠ commit rc={c.returncode}: {c.stdout[:200]}")

        p = subprocess.run(
            ["git", "push"],
            check=False, capture_output=True, text=True,
        )
        if p.returncode != 0:
            print(f"    ⚠ push rc={p.returncode}: {p.stderr[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"    ⚠ Error commit/push: {exc}")
        return False


# ============================================================
# TELEGRAM — DESCARGA
# ============================================================

def create_telegram_client() -> TelegramClient:
    return TelegramClient(
        StringSession(TELEGRAM_SESSION_STR),
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=3,
        timeout=60,
        request_retries=5,
    )


async def download_media_with_retry(client, message_id, local_path):
    for attempt in range(1, 4):
        try:
            print(f"    ↓ Intento {attempt}/3...")

            message = await client.get_messages(MAIN_CHANNEL, ids=message_id)

            if not message or not message.file:
                print(f"    ⚠ Mensaje {message_id} sin archivo")
                return False

            if local_path.exists():
                try:
                    local_path.unlink()
                except OSError:
                    pass

            await client.download_media(message, file=str(local_path))

            if not local_path.exists() or local_path.stat().st_size == 0:
                raise ValueError("Archivo vacío")

            size_mb = local_path.stat().st_size / (1024 * 1024)
            print(f"    ✓ Descargado: {size_mb:.1f} MB")
            return True

        except Exception as exc:
            print(f"    ✗ Error ({type(exc).__name__}): {exc}")
            if attempt < 3:
                await asyncio.sleep(5)
            else:
                return False

    return False


# ============================================================
# IDRIVE E2 — SUBIDA Y URL PREFIRMADA
# ============================================================

def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=IDRIVE_ENDPOINT,
        aws_access_key_id=IDRIVE_ACCESS_KEY,
        aws_secret_access_key=IDRIVE_SECRET_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        region_name=IDRIVE_REGION,
    )


def subir_a_idrive(s3, ruta_local: Path, key: str) -> bool:
    try:
        s3.upload_file(str(ruta_local), IDRIVE_BUCKET, key)
        return True
    except ClientError as e:
        print(f"    ✗ Error subiendo a IDrive: {e}")
        return False


def generar_url_prefirmada(s3, key: str) -> str | None:
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": IDRIVE_BUCKET, "Key": key},
            ExpiresIn=PRESIGN_EXPIRATION,
        )
        return url
    except ClientError as e:
        print(f"    ✗ Error generando URL: {e}")
        return None


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

async def main():
    print("=" * 60)
    print("UPLOADER IDRIVE E2 — GitHub Actions")
    print("=" * 60)
    print(f"Checkpoint cada {SAVE_EVERY_N} paquetes")

    if not TELEGRAM_SESSION_STR:
        raise RuntimeError("Falta TELEGRAM_SESSION_STR.")

    if not API_ID or not API_HASH:
        raise RuntimeError("Faltan TELEGRAM_API_ID o TELEGRAM_API_HASH.")

    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    client = create_telegram_client()

    print("Conectando con Telegram...")
    await client.start()

    if not await client.is_user_authorized():
        raise RuntimeError("La sesión de Telegram no está autorizada.")

    me = await client.get_me()
    print(f"✓ Conectado como: {me.first_name} (@{me.username})")

    catalog = load_catalog()

    # --------------------------------------------------------
    # ENCONTRAR PENDIENTES
    # --------------------------------------------------------
    pending = find_packages_without_url(catalog)
    total = len(pending)
    print(f"\nPaquetes sin subir detectados: {total}")

    if total == 0:
        print("Nada que procesar.")
        await client.disconnect()
        return

    to_process = pending[:MAX_PACKAGES_PER_RUN]
    print(f"Procesando {len(to_process)} paquetes (límite: {MAX_PACKAGES_PER_RUN})")

    s3 = get_s3_client()

    success_count = 0
    fail_count = 0

    try:
        for idx, (series, pack) in enumerate(to_process, start=1):
            series_name = series.get("name", "Desconocida")
            series_id = series.get("id", "")
            pack_name = pack.get("name", "Paquete")
            message_id = pack.get("message_id")
            filename = pack.get("filename", f"{message_id}.pdf")
            is_visible = series.get("visible", True)
            tag = "" if is_visible else " [OCULTA]"

            print(f"\n[{idx}/{len(to_process)}] {series_name}{tag} → {pack_name}")

            local_file = TEMP_DOWNLOAD_DIR / sanitize_filename(filename)

            if not message_id:
                print("    ✗ Paquete sin message_id")
                fail_count += 1
                continue

            # Descargar de Telegram
            ok = await download_media_with_retry(client, message_id, local_file)

            if not ok:
                print("    ✗ Descarga fallida tras reintentos")
                fail_count += 1
                if local_file.exists():
                    try:
                        local_file.unlink()
                    except OSError:
                        pass
                continue

            # Subir a IDrive e2
            key = f"{series_id}/{message_id}_{sanitize_filename(filename)}"

            if subir_a_idrive(s3, local_file, key):
                # Generar URL prefirmada (7 días)
                url = generar_url_prefirmada(s3, key)

                if url:
                    pack["download_url"] = url
                    success_count += 1
                    print(f"    ✓ URL prefirmada generada (válida 7 días)")
                else:
                    fail_count += 1
                    print("    ✗ No se pudo generar URL")
            else:
                fail_count += 1
                print("    ✗ Falló la subida a IDrive")

            # Limpiar
            try:
                if local_file.exists():
                    local_file.unlink()
            except OSError:
                pass

            gc.collect()

            # Guardar checkpoint local
            try:
                save_catalog(catalog, silent=True)
            except Exception as exc:
                print(f"    ⚠ No se pudo guardar checkpoint: {exc}")

            # Checkpoint remoto cada N paquetes
            if idx % SAVE_EVERY_N == 0 and idx < len(to_process):
                print(f"    💾 Checkpoint remoto: {idx}/{len(to_process)}")
                if git_commit_and_push(f"IDrive: checkpoint {idx}/{len(to_process)}"):
                    print(f"    ✓ Checkpoint subido a GitHub")
                else:
                    print(f"    ⚠ Checkpoint NO subido (se reintentará al final)")

            if idx < len(to_process):
                print(f"    ⏸  Pausa de 3s...")
                await asyncio.sleep(3)
    finally:
        # Guardar y subir SIEMPRE, haya fallado o no
        print("\n" + "-" * 60)
        print("Guardado final...")
        try:
            save_catalog(catalog)
            if git_commit_and_push(
                f"IDrive: guardado final ({success_count} subidos)"
            ):
                print("✓ Catálogo final subido a GitHub")
            else:
                print("⚠ No se pudo subir el catálogo final")
        except Exception as exc:
            print(f"⚠ Error en guardado final: {exc}")

    # --------------------------------------------------------
    # REPORTE FINAL
    # --------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"✓ Subidos: {success_count}")
    print(f"✗ Fallidos: {fail_count}")
    print(f"📦 Total: {total}")
    print("=" * 60)

    # Notificar por Telegram
    try:
        report = (
            f"📊 *Uploader IDrive e2 — Reporte*\n"
            f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"✅ Subidos: {success_count}\n"
            f"❌ Fallidos: {fail_count}\n"
            f"📊 Procesados: {len(to_process)} de {total}\n"
        )
        await client.send_message(NOTIFY_USERNAME, report, parse_mode="md")
        print("✓ Reporte enviado por Telegram.")
    except Exception as exc:
        print(f"⚠ Error enviando reporte: {exc}")

    try:
        await client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelado.")
    except Exception as exc:
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}")
        sys.exit(1)
