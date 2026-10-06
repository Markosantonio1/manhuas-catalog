# uploader_buzzheavier.py
# PLAN B: sube a BuzzHeavier y guarda el ID del archivo en el catálogo.
# Incluye verificación previa de URLs existentes (auto-limpieza).
#
# Fusiona:
#   - VERIFICACIÓN de URLs existentes (auto-limpieza)
#   - Reintentos de descarga y conexión (robusto para GitHub Actions)
#   - gc.collect() entre paquetes
#   - Pausa de 3s entre paquetes
#   - finally con checkpoint

import asyncio
import gc
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

UPLOAD_TIMEOUT = 900
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY = 5
PAUSE_BETWEEN_PACKAGES = 3

# --- Verificación de URLs existentes ---
VERIFY_URLS = os.getenv("VERIFY_URLS", "1") == "1"
VERIFY_TIMEOUT = 20
VERIFY_WORKERS = 10
VERIFY_RETRY = 1


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
# VERIFICACIÓN DE URLs EXISTENTES (AUTO-LIMPIEZA)
# ============================================================

def _check_url_alive(url: str) -> str:
    """
    Devuelve:
      "alive"    → 200/206 → la URL funciona
      "dead"     → 403/404/410 → murió, hay que resubir
      "unknown"  → error de red, timeout, 5xx → NO borrar (falso positivo)
    """
    for attempt in range(VERIFY_RETRY + 1):
        try:
            headers = {
                "Range": "bytes=0-0",
                "User-Agent": "Mozilla/5.0 (Android) ManhuasApp/1.0",
            }
            r = requests.get(
                url,
                headers=headers,
                timeout=VERIFY_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )
            status = r.status_code
            r.close()

            if status in (200, 206):
                return "alive"
            if status in (403, 404, 410):
                return "dead"
            if 500 <= status < 600:
                if attempt < VERIFY_RETRY:
                    time.sleep(2 * (attempt + 1))
                    continue
                return "unknown"
            return "unknown"

        except requests.exceptions.Timeout:
            if attempt < VERIFY_RETRY:
                time.sleep(2 * (attempt + 1))
                continue
            return "unknown"
        except Exception:
            return "unknown"

    return "unknown"


def verify_and_clean_catalog(catalog: dict) -> dict:
    """
    Verifica todas las download_url existentes.
    Borra las muertas junto con su buzzheavier_id para que se resuban.
    """
    targets = []
    for list_key in ("series", "novels"):
        for series in catalog.get(list_key, []):
            for pack in series.get("packages", []):
                url = pack.get("download_url")
                if url and str(url).strip():
                    targets.append((series, pack, str(url)))

    total = len(targets)
    print(f"\n🔍 Verificando {total} URLs de descarga existentes...")

    if total == 0:
        return {"total": 0, "alive": 0, "dead": 0, "unknown": 0}

    alive = 0
    dead = 0
    unknown = 0
    dead_list = []

    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as executor:
        futures = {
            executor.submit(_check_url_alive, url): (series, pack, url)
            for series, pack, url in targets
        }

        done = 0
        for future in as_completed(futures):
            done += 1
            series, pack, url = futures[future]
            try:
                result = future.result()
            except Exception:
                result = "unknown"

            if result == "alive":
                alive += 1
            elif result == "dead":
                dead += 1
                dead_list.append((series, pack, url))
            else:
                unknown += 1

            if done % 20 == 0 or done == total:
                print(
                    f"   [{done}/{total}] "
                    f"✅ {alive} vivas · "
                    f"💀 {dead} muertas · "
                    f"⚠️  {unknown} sin verificar"
                )

    for series, pack, url in dead_list:
        pack.pop("download_url", None)
        pack.pop("buzzheavier_id", None)
        print(
            f"   💀 Muerta: {series.get('name','?')} "
            f"→ {pack.get('name','?')} (se resubirá)"
        )

    return {
        "total": total,
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
    }


# ============================================================
# CREAR CLIENTE TELEGRAM
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


# ============================================================
# DESCARGA CON REINTENTOS
# ============================================================

async def download_media_with_retry(
    client: TelegramClient,
    message_id: int,
    local_path: Path,
) -> bool:
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            print(f"    ↓ Intento {attempt}/{DOWNLOAD_MAX_ATTEMPTS}...")

            message = await client.get_messages(
                MAIN_CHANNEL,
                ids=message_id,
            )

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

        except asyncio.CancelledError:
            print(f"    ✗ Cancelado por asyncio (intento {attempt})")
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                await asyncio.sleep(DOWNLOAD_RETRY_DELAY)
            else:
                return False

        except Exception as exc:
            print(f"    ✗ Error ({type(exc).__name__}): {exc}")
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                print(f"    ⏸  Reintentando en {DOWNLOAD_RETRY_DELAY}s...")
                await asyncio.sleep(DOWNLOAD_RETRY_DELAY)
            else:
                return False

    return False


# ============================================================
# SUBIDA A BUZZHEAVIER (CON RETRY Y NOMBRE SANEADO)
# ============================================================

def _make_upload_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Android) ManhuasApp/1.0",
    })
    if BUZZHEAVIER_ACCOUNT_ID:
        session.headers["Authorization"] = f"Bearer {BUZZHEAVIER_ACCOUNT_ID}"

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["PUT", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _safe_url_name(filename: str) -> str:
    safe = sanitize_filename(filename)
    safe = re.sub(r"[#\[\]{}()<>\"'`|\\^~]", "_", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe or "archivo.pdf"


def upload_to_buzzheavier(local_path: Path, max_attempts: int = 3) -> str | None:
    filename = _safe_url_name(local_path.name)
    encoded_name = quote(filename, safe="")
    upload_url = f"https://w.buzzheavier.com/{encoded_name}"

    for attempt in range(1, max_attempts + 1):
        session = _make_upload_session()
        try:
            size_mb = local_path.stat().st_size / (1024 * 1024)
            print(f"    ↑ Intento {attempt}/{max_attempts} — {filename} ({size_mb:.1f} MB)")

            with open(local_path, "rb") as fh:
                response = session.put(upload_url, data=fh, timeout=UPLOAD_TIMEOUT)

            print(f"    → HTTP {response.status_code}")

            if response.status_code not in (200, 201):
                print(f"    ✗ HTTP {response.status_code}: {response.text[:300]}")
                if attempt < max_attempts:
                    delay = 5 * attempt
                    print(f"    ⏸  Reintentando en {delay}s...")
                    time.sleep(delay)
                    continue
                return None

            try:
                data = response.json()
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                file_id = inner.get("id") if isinstance(inner, dict) else None
                if not file_id and isinstance(data, dict):
                    file_id = data.get("id")

                if file_id:
                    print(f"    ✓ ID obtenido: {file_id}")
                    return str(file_id)

                print(f"    ✗ No se encontró 'id' en: {data}")
                return None

            except json.JSONDecodeError:
                print(f"    ✗ Respuesta no JSON: {response.text[:200]}")
                return None

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            print(f"    ✗ Error de red ({type(exc).__name__}): {exc}")
            if attempt < max_attempts:
                delay = 5 * attempt
                print(f"    ⏸  Reintentando en {delay}s...")
                time.sleep(delay)
                continue
            return None

        except Exception as exc:
            print(f"    ✗ Error inesperado: {type(exc).__name__}: {exc}")
            return None

        finally:
            try:
                session.close()
            except Exception:
                pass

    return None


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

async def main():
    print("========================================")
    print("UPLOADER BUZZHEAVIER — GitHub Actions")
    print("(Verificación + reintentos + checkpoint)")
    print("========================================")

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
    # FASE 1: VERIFICAR URLs EXISTENTES
    # --------------------------------------------------------
    verify_stats = {"total": 0, "alive": 0, "dead": 0, "unknown": 0}

    if VERIFY_URLS:
        try:
            verify_stats = verify_and_clean_catalog(catalog)

            if verify_stats["dead"] > 0:
                save_catalog_output(catalog)
                print(f"✓ {verify_stats['dead']} URLs muertas eliminadas del catálogo")
        except Exception as exc:
            print(f"⚠ Error verificando URLs: {type(exc).__name__}: {exc}")
            print("   Continuando sin verificación...")
    else:
        print("ℹ Verificación de URLs desactivada (VERIFY_URLS=0)")

    # --------------------------------------------------------
    # FASE 2: PROCESAR PENDIENTES
    # --------------------------------------------------------
    pending = find_packages_without_url(catalog)
    total = len(pending)
    print(f"\nPaquetes sin subir detectados: {total}")

    if total == 0:
        print("Nada que procesar. Enviando reporte igual.")

        try:
            await send_report(
                client,
                verify_stats,
                success_count=0,
                fail_count=0,
                processed=0,
                total=0,
                remaining=0,
                series_stats={},
            )
        except Exception as exc:
            print(f"⚠ Error enviando reporte: {exc}")

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

            if not message_id:
                print("    ✗ Paquete sin message_id")
                series_stats[series_id]["fail"] += 1
                fail_count += 1
                continue

            ok = await download_media_with_retry(
                client,
                message_id,
                local_file,
            )

            if not ok:
                print("    ✗ Descarga fallida tras reintentos")
                series_stats[series_id]["fail"] += 1
                fail_count += 1
                if local_file.exists():
                    try:
                        local_file.unlink()
                    except OSError:
                        pass
                continue

            buzz_id = upload_to_buzzheavier(local_file)

            if buzz_id:
                pack["buzzheavier_id"] = buzz_id
                series_stats[series_id]["success"] += 1
                success_count += 1
                print(f"    ✓ ID GUARDADO: {buzz_id}")
            else:
                series_stats[series_id]["fail"] += 1
                fail_count += 1
                print("    ✗ FALLÓ LA SUBIDA")

            try:
                if local_file.exists():
                    local_file.unlink()
            except OSError:
                pass

            gc.collect()

            try:
                save_catalog_output(catalog)
            except Exception as exc:
                print(f"    ⚠ No se pudo guardar checkpoint: {exc}")

            if idx < len(to_process):
                print(f"    ⏸  Pausa de {PAUSE_BETWEEN_PACKAGES}s...")
                await asyncio.sleep(PAUSE_BETWEEN_PACKAGES)

    finally:
        try:
            save_catalog_output(catalog)
            print("✓ Checkpoint final guardado.")
        except Exception as exc:
            print(f"⚠ No se pudo guardar checkpoint final: {exc}")

        remaining = total - len(to_process)

        try:
            await send_report(
                client,
                verify_stats,
                success_count=success_count,
                fail_count=fail_count,
                processed=len(to_process),
                total=total,
                remaining=remaining,
                series_stats=series_stats,
            )
        except Exception as exc:
            print(f"⚠ Error al enviar reporte: {exc}")

        try:
            await client.disconnect()
        except Exception:
            pass

    print("\n========================================")
    print("PROCESO COMPLETADO")
    print("========================================")


async def send_report(
    client: TelegramClient,
    verify_stats: dict,
    success_count: int,
    fail_count: int,
    processed: int,
    total: int,
    remaining: int,
    series_stats: dict,
):
    report_lines = [
        "📊 *Uploader BuzzHeavier — Reporte*",
        f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    if verify_stats.get("total", 0) > 0:
        report_lines.append("🔍 *Verificación de URLs existentes:*")
        report_lines.append(f"   Total: {verify_stats['total']}")
        report_lines.append(f"   ✅ Vivas: {verify_stats['alive']}")
        report_lines.append(f"   💀 Muertas (se resubirán): {verify_stats['dead']}")
        report_lines.append(f"   ⚠️  Sin verificar (red): {verify_stats['unknown']}")
        report_lines.append("")

    report_lines.append("📦 *Subida de nuevos:*")
    report_lines.append(f"   ✅ Subidos: {success_count}")
    report_lines.append(f"   ❌ Fallidos: {fail_count}")
    report_lines.append(f"   📊 Procesados: {processed} de {total}")

    if remaining > 0:
        report_lines.append(f"   ⏳ Pendientes para próximas corridas: {remaining}")

    if series_stats:
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
        "➡ Descarga el archivo adjunto y ejecútalo con `enlace.py`."
    )

    report = "\n".join(report_lines)

    print(f"\nEnviando reporte a {NOTIFY_USERNAME}...")
    await client.send_message(NOTIFY_USERNAME, report, parse_mode="md")
    print("✓ Reporte enviado.")

    await client.send_file(
        NOTIFY_USERNAME,
        str(CATALOG_OUTPUT_FILE),
        caption=(
            "📎 Catálogo con IDs de BuzzHeavier.\n"
            "Descárgalo, renómbralo a `catalog_con_ids.json` "
            "y ejecuta `enlace.py` en tu PC."
        ),
    )
    print("✓ Catálogo enviado.")


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
