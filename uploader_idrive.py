# uploader_idrive.py
import asyncio
import gc
import html
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
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

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

# Canales de Telegram
MAIN_CHANNEL = "manhuasgratis"           # manhuas normales
NOVELS_CHANNEL = -1004334091126          # novelas
ADULT_CHANNEL = -1004467622375           # +18
RESPALDO_CHANNEL = -1004367557958        # canal de respaldo (series importadas)

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))

# ⏱️ Tiempo máximo de ejecución por run (segundos). Al superarlo, el script
# para limpio, guarda progreso y dispara el siguiente run del workflow.
# 9000s = 2h 30m (margen de 30 min bajo el límite de 3h de GitHub Actions).
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "9000"))

# 🔁 Cuántos runs puede encadenar como máximo este proceso.
# 40 runs * 2h 30m = 100 horas totales.
MAX_CHAIN_RUNS = int(os.getenv("MAX_CHAIN_RUNS", "40"))

# 🔢 Número del run actual dentro de la cadena (0 = primero).
CHAIN_RUN = int(os.getenv("CHAIN_RUN", "0"))

# Nombre del workflow que se dispara para el siguiente run.
# Debe coincidir con el archivo .github/workflows/uploader_idrive.yml
UPLOADER_WORKFLOW_FILE = os.getenv(
    "UPLOADER_WORKFLOW_FILE", "uploader_idrive.yml"
)

# Cada cuántos paquetes se hace checkpoint (commit + push remoto)
SAVE_EVERY_N = int(os.getenv("SAVE_EVERY_N", "10"))

# Prefirmadas duran máximo 7 días (604800 segundos)
PRESIGN_EXPIRATION = 604800

TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_idrive_uploads"


# ============================================================
# UTILIDADES
# ============================================================

def sanitize_filename(value: str) -> str:
    """
    ⚠ IMPORTANTE: mantener IDÉNTICA a refresh_urls_idrive.py.
    Si cambia aquí, cambiar también allí.
    """
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
        print("✓ Catálogo guardado")


def compute_key(
    series_id: str,
    message_id: int,
    filename: str,
    is_novel: bool = False,
) -> str:
    """Calcula la key que tendría este paquete en IDrive."""
    prefix = "novels/" if is_novel else ""
    return f"{prefix}{series_id}/{message_id}_{sanitize_filename(filename)}"


def is_adult_series(series_id: str) -> bool:
    """Las series +18 terminan su id en '_adult'."""
    return bool(series_id) and series_id.endswith("_adult")


def is_respaldo_series(series: dict) -> bool:
    """
    Las series con género 'Respaldo' viven en el canal de respaldo.
    Viene así desde catalog_series.txt: '#X|hidden|Respaldo,Finalizada'
    y se guarda en catalog.json como "genres": ["Respaldo", "Finalizada", ...]
    """
    genres = series.get("genres", []) or []
    for g in genres:
        if str(g).strip().lower() == "respaldo":
            return True
    return False


# ============================================================
# GIT — CHECKPOINTS REMOTOS
# ============================================================

def git_commit_and_push(mensaje: str) -> bool:
    """Hace git config + add + commit + pull -X ours + push."""
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
        subprocess.run(
            ["git", "commit", "-m", mensaje, "--allow-empty"],
            check=False, capture_output=True, text=True,
        )

        # 🛡️ Usamos pull -X ours para evitar el conflicto.
        # Si hay diferencia entre el catálogo local y el de GitHub,
        # gana el local (el que tiene las URLs nuevas de IDrive).
        pr = subprocess.run(
            ["git", "pull", "origin", "main", "-X", "ours", "--no-rebase"],
            check=False, capture_output=True, text=True,
        )
        if pr.returncode != 0:
            print(f"    ⚠ pull rc={pr.returncode}: {pr.stderr[:300]}")
            return False

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
# AUTO-VISIBILIDAD — marcar series completadas como visibles
# ============================================================

def mark_completed_series_visible(catalog: dict) -> int:
    """
    Recorre el catálogo y marca como visible cualquier serie/novela
    que tenga TODOS sus paquetes con download_url asignada.

    Así, cuando este run termina de subir la última URL de una serie,
    la APK la ve sin necesidad de ejecutar todo.py.

    Devuelve el número de series que pasaron de ocultas a visibles.
    """
    changed = 0
    for list_key in ("series", "novels"):
        for item in catalog.get(list_key, []):
            packages = item.get("packages", [])
            if not packages:
                continue  # series sin paquetes → ya se ocultan en todo.py
            # ¿Todos los paquetes tienen URL?
            if not all(p.get("download_url") for p in packages):
                continue
            # ¿Ya está visible? → nada que hacer
            if item.get("visible", True):
                continue
            # Marcarla visible
            item["visible"] = True
            changed += 1
            hashtag = item.get("hashtag") or item.get("id") or "?"
            print(f"  👁️ {hashtag} → visible (todas las URLs asignadas)")
    return changed


# ============================================================
# CHAIN — disparar el siguiente run
# ============================================================

def trigger_next_run(current_chain_run: int) -> bool:
    """
    Dispara el siguiente run del workflow vía `gh workflow run`.
    Pasa el mismo MAX_PACKAGES_PER_RUN y el chain_run incrementado.
    Reintenta 2 veces si falla (rate limit puntual, red, etc.).
    """
    next_run = current_chain_run + 1
    if next_run >= MAX_CHAIN_RUNS:
        print(
            f"  ⛔ Límite de cadena alcanzado "
            f"({next_run} ≥ {MAX_CHAIN_RUNS}). NO se dispara siguiente run."
        )
        return False

    cmd = [
        "gh", "workflow", "run", UPLOADER_WORKFLOW_FILE,
        "-f", f"chain_run={next_run}",
        "-f", f"max_packages={MAX_PACKAGES_PER_RUN}",
    ]

    for attempt in range(1, 3):
        try:
            print(f"  🔁 Intento {attempt}/2: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if result.returncode == 0:
                print(f"  ✓ Siguiente run disparado (chain_run={next_run})")
                return True
            stderr_preview = (result.stderr or "").strip()[:300]
            print(f"  ⚠ gh devolvió rc={result.returncode}: {stderr_preview}")
        except Exception as exc:
            print(f"  ⚠ Error disparando: {type(exc).__name__}: {exc}")

        if attempt < 2:
            time.sleep(8)

    print(f"  ✗ No se pudo disparar el siguiente run tras 2 intentos.")
    print(f"    ⚠️ Deberás lanzarlo manualmente con:")
    print(f"       gh workflow run {UPLOADER_WORKFLOW_FILE} "
          f"-f chain_run={next_run} -f max_packages={MAX_PACKAGES_PER_RUN}")
    return False


# ============================================================
# IDRIVE E2 — CLIENTE Y OPERACIONES
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


def list_all_keys_in_bucket(s3) -> set:
    keys = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=IDRIVE_BUCKET):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


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
# RECONCILIACIÓN — detectar qué está en IDrive pero sin URL
# ============================================================

def reconcile_catalog_with_idrive(s3, catalog: dict) -> tuple:
    """
    Recorre el catálogo y comprueba cada paquete contra las keys existentes
    en IDrive.

    Procesa MANHUAS (lista "series") y NOVELAS (lista "novels").

    Devuelve (reconciliados, pendientes_reales):
      - reconciliados: nº de paquetes que ya estaban en IDrive y se les
        ha generado la URL (sin re-subir)
      - pendientes_reales: lista de (series, pack, list_key) que NO están
        en IDrive y que hay que subir de verdad
    """
    print("\n" + "=" * 60)
    print("RECONCILIACIÓN CON IDRIVE (manhuas + novelas + +18 + respaldo)")
    print("=" * 60)

    try:
        print("Listando archivos ya existentes en IDrive...")
        existing_keys = list_all_keys_in_bucket(s3)
        print(f"  → {len(existing_keys)} archivos encontrados en el bucket")
    except Exception as exc:
        print(f"  ✗ Error listando IDrive: {exc}")
        print("  ⚠ Abortando para no duplicar subidas.")
        raise

    reconciliados = 0
    ya_con_url = 0
    pendientes_reales = []

    for list_key in ("series", "novels"):
        is_novel = (list_key == "novels")
        for series in catalog.get(list_key, []):
            series_id = series.get("id", "").strip()
            if not series_id:
                continue
            for pack in series.get("packages", []):
                message_id = pack.get("message_id")
                filename = pack.get("filename", "")

                if not message_id:
                    continue

                expected_key = compute_key(
                    series_id,
                    int(message_id),
                    filename,
                    is_novel=is_novel,
                )

                if expected_key in existing_keys:
                    current_url = pack.get("download_url") or ""
                    es_url_idrive = "idrivee2" in str(current_url)
                    if not current_url or not es_url_idrive:
                        url = generar_url_prefirmada(s3, expected_key)
                        if url:
                            pack["download_url"] = url
                            reconciliados += 1
                            if reconciliados <= 20:
                                tipo = tipo_de_serie(series, list_key)
                                print(
                                    f"  ↻ [{tipo} {series_id}] "
                                    f"msg {message_id}: URL regenerada"
                                )
                    else:
                        ya_con_url += 1
                else:
                    pendientes_reales.append((series, pack, list_key))

    print()
    print(f"  ✓ Reconciliados (estaban sin URL o migrados): {reconciliados}")
    print(f"  • Ya tenían URL: {ya_con_url}")
    print(f"  • Pendientes reales (subir): {len(pendientes_reales)}")

    if reconciliados > 0:
        print(f"\n  💾 Guardando {reconciliados} URLs nuevas en catalog.json...")
        save_catalog(catalog, silent=True)

    return reconciliados, pendientes_reales


# ============================================================
# TELEGRAM — DESCARGA
# ============================================================

def create_telegram_client() -> TelegramClient:
    if TELEGRAM_SESSION_STR:
        session = StringSession(TELEGRAM_SESSION_STR)
    else:
        session = str(BASE_DIR / "manhuas_downloader")
    return TelegramClient(
        session,
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=3,
        timeout=60,
        request_retries=5,
    )


async def download_media_with_retry(client, message_id, local_path, channel):
    for attempt in range(1, 4):
        try:
            print(f"    ↓ Intento {attempt}/3...")

            message = await client.get_messages(channel, ids=message_id)

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
# UTILIDADES DE CLASIFICACIÓN
# ============================================================

def tipo_de_serie(series: dict, list_key: str) -> str:
    """
    Devuelve un string con el tipo de serie para los logs:
    'manhua', '+18', 'respaldo' o 'novela'.
    """
    series_id = series.get("id", "")
    if list_key == "novels":
        return "novela"
    if is_adult_series(series_id):
        return "+18"
    if is_respaldo_series(series):
        return "respaldo"
    return "manhua"


def elegir_canal(series: dict, list_key: str) -> tuple:
    """
    Devuelve (channel_id_o_username, nombre_canal_para_logs) para descargar
    los PDFs de esta serie desde el canal correcto.
    """
    series_id = series.get("id", "")

    # 🛡️ Si es de Respaldo, sus PDFs SIEMPRE están en el canal de
    # respaldo, aunque también tenga el género "+18".
    # Por eso comprobamos "Respaldo" ANTES que "+18".
    if is_respaldo_series(series):
        return RESPALDO_CHANNEL, "respaldo"

    if is_adult_series(series_id):
        return ADULT_CHANNEL, "+18"

    if list_key == "novels":
        return NOVELS_CHANNEL, "novelas"

    return MAIN_CHANNEL, "principal"


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

async def main():
    # ⏱️ Marca de tiempo de inicio — para el corte por tiempo
    START_TIME = time.time()

    print("=" * 60)
    print("UPLOADER IDRIVE E2 — GitHub Actions (manhuas + novelas + +18 + respaldo)")
    print("=" * 60)
    print(f"Checkpoint cada {SAVE_EVERY_N} paquetes")
    print(f"Chain run: {CHAIN_RUN}/{MAX_CHAIN_RUNS - 1}")
    print(f"Tiempo máximo: {MAX_RUNTIME_SECONDS}s ({MAX_RUNTIME_SECONDS // 60} min)")

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
    # FASE 1 — RECONCILIACIÓN
    # --------------------------------------------------------
    s3 = get_s3_client()

    try:
        reconciliados, pendientes_reales = reconcile_catalog_with_idrive(s3, catalog)
    except Exception as exc:
        print(f"\n✗ Reconciliación falló: {exc}")
        print("  Abortando para no re-subir a ciegas.")
        try:
            await client.disconnect()
        except Exception:
            pass
        sys.exit(1)

    if reconciliados > 0:
        if git_commit_and_push(
            f"IDrive: reconciliar {reconciliados} URLs existentes"
        ):
            print("  ✓ Commit de reconciliación subido a GitHub")
        else:
            print("  ⚠ No se pudo subir el commit de reconciliación")

    # Contadores por tipo
    total_pendientes = len(pendientes_reales)
    pendientes_manhuas = 0
    pendientes_adult = 0
    pendientes_respaldo = 0
    pendientes_novelas = 0

    for (s, _, lk) in pendientes_reales:
        tipo = tipo_de_serie(s, lk)
        if tipo == "manhua":
            pendientes_manhuas += 1
        elif tipo == "+18":
            pendientes_adult += 1
        elif tipo == "respaldo":
            pendientes_respaldo += 1
        elif tipo == "novela":
            pendientes_novelas += 1

    print(f"\nPaquetes reales por subir: {total_pendientes}")
    print(f"  • Manhuas:  {pendientes_manhuas}")
    print(f"  • +18:      {pendientes_adult}")
    print(f"  • Respaldo: {pendientes_respaldo}")
    print(f"  • Novelas:  {pendientes_novelas}")

    # --------------------------------------------------------
    # FASE 2 — SUBIDA REAL
    # --------------------------------------------------------
    if total_pendientes == 0:
        print("Nada que subir. Fin.")
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    to_process = pendientes_reales[:MAX_PACKAGES_PER_RUN]
    print(f"Procesando {len(to_process)} paquetes (límite: {MAX_PACKAGES_PER_RUN})")

    success_count = 0
    fail_count = 0
    timed_out = False
    newly_visible = 0

    # 🚀 MENSAJE DE PROGRESO: Enviamos el mensaje inicial y guardamos su ID
    progress_message_id = None
    last_update_time = time.time()
    try:
        initial_text = (
            "🤖 <b>Bot de subida IDrive en acción</b>\n\n"
            f"[░░░░░░░░░░░░░░░░░░░░] 0% (0/{len(to_process)})\n\n"
            f"📚 <b>Serie:</b> Iniciando...\n"
            f"📄 <b>Actual:</b> Esperando...\n"
            f"✅ <b>Éxitos:</b> 0 | ❌ <b>Errores:</b> 0"
        )
        msg = await client.send_message(NOTIFY_USERNAME, initial_text, parse_mode="html")
        progress_message_id = msg.id
    except Exception as e:
        print(f"⚠️ No se pudo enviar el mensaje de progreso: {e}")

    try:
        for idx, (series, pack, list_key) in enumerate(to_process, start=1):
            # ⏱️ CORTE POR TIEMPO: comprobar ANTES de procesar el siguiente
            # paquete. Si ya hemos superado el tiempo máximo, salimos limpios
            # sin cortar una descarga/subida a medias.
            elapsed = time.time() - START_TIME
            if elapsed >= MAX_RUNTIME_SECONDS:
                print(
                    f"\n⏸️ Tiempo límite alcanzado "
                    f"({elapsed:.0f}s ≥ {MAX_RUNTIME_SECONDS}s). "
                    f"Deteniendo para guardar progreso y encadenar siguiente run."
                )
                print(f"   Procesados en este run: {idx - 1}/{len(to_process)}")
                print(f"   Pendientes después de este run: "
                      f"{total_pendientes - success_count}")
                timed_out = True
                break

            series_name = series.get("name", "Desconocida")
            series_id = series.get("id", "")
            pack_name = pack.get("name", "Paquete")
            message_id = pack.get("message_id")
            filename = pack.get("filename", f"{message_id}.pdf")
            is_visible = series.get("visible", True)
            is_novel = (list_key == "novels")

            tipo = tipo_de_serie(series, list_key)

            tag = "" if is_visible else " [OCULTA]"
            if tipo == "novela":
                tipo_tag = " [NOVELA]"
            elif tipo == "+18":
                tipo_tag = " [+18]"
            elif tipo == "respaldo":
                tipo_tag = " [RESPALDO]"
            else:
                tipo_tag = ""

            print(f"\n[{idx}/{len(to_process)}] {series_name}{tipo_tag}{tag} → {pack_name}")

            local_file = TEMP_DOWNLOAD_DIR / sanitize_filename(filename)

            if not message_id:
                print("    ✗ Paquete sin message_id")
                fail_count += 1
                continue

            # 📡 Elegir canal correcto según el tipo de serie
            channel, canal_tipo = elegir_canal(series, list_key)
            print(f"    📡 Canal: {canal_tipo} ({channel})")

            # Descargar de Telegram
            ok = await download_media_with_retry(client, message_id, local_file, channel)

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
            key = compute_key(series_id, int(message_id), filename, is_novel=is_novel)

            if subir_a_idrive(s3, local_file, key):
                url = generar_url_prefirmada(s3, key)
                if url:
                    pack["download_url"] = url
                    pack["download_ready"] = True
                    success_count += 1
                    print("    ✓ URL prefirmada generada (válida 7 días)")
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

            # Checkpoint local
            try:
                save_catalog(catalog, silent=True)
            except Exception as exc:
                print(f"    ⚠ No se pudo guardar checkpoint: {exc}")

            # Checkpoint remoto
            if idx % SAVE_EVERY_N == 0 and idx < len(to_process):
                print(f"    💾 Checkpoint remoto: {idx}/{len(to_process)}")
                if git_commit_and_push(
                    f"IDrive: checkpoint {idx}/{len(to_process)}"
                ):
                    print("    ✓ Checkpoint subido a GitHub")
                else:
                    print("    ⚠ Checkpoint NO subido (se reintentará al final)")

            # ⏱️ ACTUALIZACIÓN DEL PROGRESO (cada 20 segundos)
            if progress_message_id and (time.time() - last_update_time >= 20):
                try:
                    percent = int((idx / len(to_process)) * 100)
                    filled = int(percent / 5)
                    bar = "█" * filled + "░" * (20 - filled)

                    safe_series = html.escape(series_name + tipo_tag + tag)
                    safe_pack = html.escape(pack_name)

                    progress_text = (
                        "🤖 <b>Bot de subida IDrive en acción</b>\n\n"
                        f"[{bar}] {percent}% ({idx}/{len(to_process)})\n\n"
                        f"📚 <b>Serie:</b> {safe_series}\n"
                        f"📄 <b>Actual:</b> {safe_pack}\n"
                        f"✅ <b>Éxitos:</b> {success_count} | ❌ <b>Errores:</b> {fail_count}"
                    )

                    await client.edit_message(
                        NOTIFY_USERNAME, progress_message_id, progress_text, parse_mode="html"
                    )
                    last_update_time = time.time()
                    print("    🔄 Mensaje de progreso actualizado en Telegram.")
                except Exception as e:
                    print(f"    ⚠️ No se pudo actualizar el mensaje: {e}")

            if idx < len(to_process):
                print("    ⏸  Pausa de 3s...")
                await asyncio.sleep(3)
    finally:
        print("\n" + "-" * 60)
        print("Guardado final...")

        # 1. 👁️ Marcar como visibles las series que ya tienen todas las URLs.
        #    Esto se hace ANTES del save_catalog para que el cambio se suba
        #    en el mismo commit.
        try:
            newly_visible = mark_completed_series_visible(catalog)
            if newly_visible > 0:
                print(f"  ✓ {newly_visible} series ahora visibles")
        except Exception as exc:
            print(f"  ⚠ Error marcando visibles: {exc}")

        # 2. 💾 Guardar + push (incluye las visibles recién marcadas).
        try:
            save_catalog(catalog)
            commit_msg = f"IDrive: guardado final ({success_count} subidos"
            if newly_visible > 0:
                commit_msg += f", {newly_visible} visibles"
            if timed_out:
                commit_msg += ", detenido por tiempo"
            commit_msg += f") [chain {CHAIN_RUN}]"

            if git_commit_and_push(commit_msg):
                print("✓ Catálogo final subido a GitHub")
            else:
                print("⚠ No se pudo subir el catálogo final")
        except Exception as exc:
            print(f"⚠ Error en guardado final: {exc}")

    # --------------------------------------------------------
    # REPORTE FINAL
    # --------------------------------------------------------
    pending_after = total_pendientes - success_count
    made_progress = (success_count + reconciliados) > 0

    # 🔁 Decidir si hay que encadenar el siguiente run
    should_chain = (
        timed_out                       # solo si se cortó por tiempo
        and pending_after > 0           # y queda trabajo pendiente
        and made_progress               # y este run sí progresó
        and CHAIN_RUN < MAX_CHAIN_RUNS  # y no se ha alcanzado el límite
    )

    print("\n" + "=" * 60)
    print(f"✓ Reconciliados: {reconciliados}")
    print(f"✓ Subidos nuevos: {success_count}")
    print(f"✗ Fallidos: {fail_count}")
    print(f"👁️ Marcadas visibles: {newly_visible}")
    print(f"📦 Pendientes antes (manhuas):  {pendientes_manhuas}")
    print(f"📦 Pendientes antes (+18):      {pendientes_adult}")
    print(f"📦 Pendientes antes (respaldo): {pendientes_respaldo}")
    print(f"📦 Pendientes antes (novelas):  {pendientes_novelas}")
    print(f"📦 Pendientes restantes: {pending_after}")
    print(f"⏱️ Duración: {time.time() - START_TIME:.0f}s "
          f"· Chain: {CHAIN_RUN}/{MAX_CHAIN_RUNS - 1}")
    if timed_out:
        print(f"⏸️ Detenido por tiempo")
        if should_chain:
            print(f"🔁 Encadenando siguiente run (chain_run={CHAIN_RUN + 1})...")
        elif not made_progress:
            print(f"⛔ Sin progreso → NO se encadena")
        elif CHAIN_RUN >= MAX_CHAIN_RUNS:
            print(f"⛔ Límite de {MAX_CHAIN_RUNS} runs alcanzado → NO se encadena")
    elif pending_after == 0:
        print(f"✅ TODOS los pendientes completados. Fin del proceso.")
    print("=" * 60)

    try:
        # 🏷️ Línea de estado del chain
        if timed_out and should_chain:
            chain_status = (
                f"\n⏸️ <b>Detenido por tiempo</b> (2h 30m)\n"
                f"🔁 Encadenando siguiente run "
                f"(chain_run={CHAIN_RUN + 1}/{MAX_CHAIN_RUNS - 1})..."
            )
        elif timed_out and not made_progress:
            chain_status = (
                f"\n⛔ Detenido por tiempo <b>sin progreso</b> "
                f"(sin conexión o error). NO se encadena."
            )
        elif timed_out and CHAIN_RUN >= MAX_CHAIN_RUNS:
            chain_status = (
                f"\n⛔ Límite de {MAX_CHAIN_RUNS} runs alcanzado. "
                f"NO se encadena. Ejecuta manualmente."
            )
        elif pending_after == 0:
            chain_status = f"\n✅ <b>Todos los pendientes completados</b>"
        else:
            chain_status = ""

        visible_line = (
            f"\n👁️ Nuevas series visibles: {newly_visible}"
            if newly_visible > 0 else ""
        )

        report = (
            f"📊 <b>Reporte Final - Uploader IDrive e2</b>\n"
            f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Chain run: {CHAIN_RUN}/{MAX_CHAIN_RUNS - 1}\n\n"
            f"↻ Reconciliados: {reconciliados}\n"
            f"✅ Subidos nuevos: {success_count}\n"
            f"❌ Fallidos: {fail_count}\n"
            f"{visible_line}\n"
            f"📦 Pendientes restantes: {pending_after}\n"
            f"  • Manhuas:  {pendientes_manhuas}\n"
            f"  • +18:      {pendientes_adult}\n"
            f"  • Respaldo: {pendientes_respaldo}\n"
            f"  • Novelas:  {pendientes_novelas}\n"
            f"{chain_status}"
        )

        if progress_message_id:
            await client.edit_message(
                NOTIFY_USERNAME, progress_message_id, report, parse_mode="html"
            )
            print("✓ Reporte final enviado (editado sobre el progreso).")
        else:
            await client.send_message(NOTIFY_USERNAME, report, parse_mode="html")
            print("✓ Reporte enviado por Telegram (mensaje nuevo).")

    except Exception as exc:
        print(f"⚠ Error enviando reporte: {exc}")

    # --------------------------------------------------------
    # 🔁 DISPARAR SIGUIENTE RUN (si aplica)
    # --------------------------------------------------------
    if should_chain:
        print("\n" + "=" * 60)
        print(f"🔁 Encadenando siguiente run...")
        trigger_next_run(CHAIN_RUN)
        print("=" * 60)

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
