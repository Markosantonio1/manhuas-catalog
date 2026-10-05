import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urljoin, urlparse

import requests
from curl_cffi import requests as curl_requests   # ← NUEVO: impersona Chrome
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

BUZZHEAVIER_ACCOUNT_ID = os.getenv("BUZZHEAVIER_ACCOUNT_ID", "").strip()

NOTIFY_USERNAME = "@Markosantonio"
MAIN_CHANNEL = "manhuasgratis"

MAX_PACKAGES_PER_RUN = int(os.getenv("MAX_PACKAGES_PER_RUN", "5"))

HTTP_TIMEOUT = 60
UPLOAD_TIMEOUT = 900
DIRECT_LINK_ATTEMPTS = 3
DIRECT_LINK_RETRY_DELAY = 1.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# Versión de Chrome a impersonar con curl_cffi
IMPERSONATE_TARGET = "chrome120"


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
    CATALOG_OUTPUT_FILE.write_text(
        json.dumps(
            catalog,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"✓ Catálogo actualizado guardado en: {CATALOG_OUTPUT_FILE}"
    )


def find_packages_without_url(catalog: dict) -> list:
    pending = []

    for list_key in ("series", "novels"):
        for series in catalog.get(list_key, []):
            for pack in series.get("packages", []):
                url = pack.get("download_url")

                if not url or not str(url).strip():
                    pending.append((series, pack))

    return pending


def normalize_url(value: str, base_url: str) -> str | None:
    if not value:
        return None

    value = str(value).strip()
    value = value.replace("&amp;", "&")
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&")

    try:
        return urljoin(base_url, value)
    except Exception:
        return None


def unique_urls(values: list[str]) -> list[str]:
    result = []
    seen = set()

    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result


# ============================================================
# BUZZHEAVIER - VALIDACIÓN DE URL DIRECTA
# ============================================================

def _is_buzzheavier_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")

    return (
        host == "buzzheavier.com"
        or host.endswith(".buzzheavier.com")
        or host == "bzzhr.to"
        or host.endswith(".bzzhr.to")
    )


def is_direct_download_url(url: str) -> bool:
    """
    BuzzHeavier entrega enlaces firmados parecidos a:

        https://<download-host>/d/<file-id>?v=<signed-token>

    La página normal https://buzzheavier.com/<id> NO es un
    enlace directo y nunca se guarda como download_url.
    """

    if not url:
        return False

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        query = parsed.query or ""

        if parsed.scheme not in ("http", "https"):
            return False

        if not _is_buzzheavier_host(host):
            return False

        if not re.search(r"/d/[^/]+", path, flags=re.IGNORECASE):
            return False

        # El enlace copiado por BuzzHeavier usa token firmado.
        if not query:
            return False

        params = dict()
        for item in query.split("&"):
            if "=" in item:
                key, value = item.split("=", 1)
                params[key.lower()] = value

        return bool(params.get("v"))

    except Exception:
        return False


# ============================================================
# BUZZHEAVIER - EXTRAER ENDPOINTS DE LA PÁGINA
# ============================================================

def extract_download_endpoints_from_html(
    html: str,
    page_url: str,
) -> list[str]:
    """
    Reproduce el método usado por automatizadores actuales:

    1. busca a[hx-get*="/download"];
    2. busca cualquier elemento que tenga copyDownloadLink(...);
    3. normaliza las rutas a URLs absolutas.

    No construimos a ciegas solamente /<id>/download porque la
    página puede incluir un parámetro ?t=... necesario para el
    servidor.
    """

    endpoints = []

    # --------------------------------------------------------
    # hx-get="/ID/download?t=..."
    # --------------------------------------------------------
    hx_patterns = [
        re.compile(
            r'''hx-get\s*=\s*["']([^"']*?/download(?:\?[^"']*)?)["']''',
            re.IGNORECASE,
        ),
        re.compile(
            r'''data-hx-get\s*=\s*["']([^"']*?/download(?:\?[^"']*)?)["']''',
            re.IGNORECASE,
        ),
    ]

    for pattern in hx_patterns:
        for match in pattern.finditer(html):
            endpoint = normalize_url(
                match.group(1),
                page_url,
            )
            if endpoint:
                endpoints.append(endpoint)

    # --------------------------------------------------------
    # onclick="copyDownloadLink('...')"
    # --------------------------------------------------------
    onclick_pattern = re.compile(
        r'''copyDownloadLink\s*\(\s*["']([^"']+)["']\s*\)''',
        re.IGNORECASE,
    )

    for match in onclick_pattern.finditer(html):
        endpoint = normalize_url(
            match.group(1),
            page_url,
        )
        if endpoint:
            endpoints.append(endpoint)

    # --------------------------------------------------------
    # Fallback muy permisivo para HTML minificado/alterado
    # --------------------------------------------------------
    fallback_pattern = re.compile(
        r'''(?:hx-get|data-hx-get)\s*=\s*([^\s>]+/download\?[^\s>]+)''',
        re.IGNORECASE,
    )

    for match in fallback_pattern.finditer(html):
        raw = match.group(1).strip("'\"` >")
        endpoint = normalize_url(raw, page_url)
        if endpoint:
            endpoints.append(endpoint)

    return unique_urls(endpoints)


def get_file_page_and_endpoints(
    file_id: str,
    session: requests.Session,
) -> tuple[str, list[str]] | None:
    """
    Abre la página pública del archivo y devuelve:

        (URL final de página, [endpoints de descarga])

    Usa curl_cffi para impersonar Chrome real y saltar Cloudflare.
    """

    page_url = f"https://buzzheavier.com/{file_id}"

    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "User-Agent": USER_AGENT,
    }

    try:
        print("    → Abriendo página de BuzzHeavier (con curl_cffi)...")

        response = curl_requests.get(
            page_url,
            headers=headers,
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
            impersonate=IMPERSONATE_TARGET,
        )

        print(
            f"    → Página HTTP {response.status_code}: {response.url}"
        )

        if response.status_code != 200:
            print(
                "    ✗ BuzzHeavier no devolvió la página correctamente."
            )
            return None

        final_page_url = response.url or page_url

        endpoints = extract_download_endpoints_from_html(
            response.text,
            final_page_url,
        )

        if not endpoints:
            print(
                "    ✗ No se encontró el endpoint /download en la página."
            )
            return None

        print(
            f"    ✓ Encontrados {len(endpoints)} endpoint(s) de descarga:"
        )

        for index, endpoint in enumerate(endpoints, start=1):
            print(f"      S{index}: {endpoint}")

        return final_page_url, endpoints

    except Exception as exc:
        print(
            f"    ✗ Error cargando página BuzzHeavier: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


# ============================================================
# BUZZHEAVIER - OBTENER ENLACE DIRECTO
# ============================================================

def request_direct_link(
    download_endpoint: str,
    referer_url: str,
    session: requests.Session,
) -> tuple[str | None, str]:
    """
    Hace exactamente el tipo de solicitud que utiliza el
    automatizador web actual de BuzzHeavier:

        GET /download?... 
        HX-Current-URL: <página del archivo>
        HX-Request: true

    Importante: NO seguimos automáticamente redirects. Esto
    evita seguir la publicidad en vez de capturar el enlace.

    También usa curl_cffi para saltar Cloudflare.
    """

    headers = {
        "Accept": "*/*",
        "HX-Current-URL": referer_url.rstrip("/"),
        "HX-Request": "true",
        "Referer": referer_url.rstrip("/"),
        "User-Agent": USER_AGENT,
    }

    try:
        response = curl_requests.get(
            download_endpoint,
            headers=headers,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
            impersonate=IMPERSONATE_TARGET,
        )

        print(
            f"      → /download respondió HTTP {response.status_code}"
        )

        # ----------------------------------------------------
        # 1. HX-Redirect: método principal
        # ----------------------------------------------------
        hx_redirect = (
            response.headers.get("HX-Redirect")
            or response.headers.get("hx-redirect")
        )

        if hx_redirect:
            candidate = normalize_url(
                hx_redirect,
                download_endpoint,
            )

            if candidate:
                if is_direct_download_url(candidate):
                    return candidate, "HX-Redirect"

                print(
                    f"      ⚠ HX-Redirect no es directo: {candidate}"
                )

        # ----------------------------------------------------
        # 2. Location: útil en implementaciones alternativas
        # ----------------------------------------------------
        location = (
            response.headers.get("Location")
            or response.headers.get("location")
        )

        if location:
            candidate = normalize_url(
                location,
                download_endpoint,
            )

            if candidate:
                if is_direct_download_url(candidate):
                    return candidate, "Location"

                print(
                    f"      ⚠ Location no es directo: {candidate}"
                )

        # ----------------------------------------------------
        # 3. A veces el servidor puede devolver la URL en HTML/texto
        # ----------------------------------------------------
        body = response.text or ""

        body_patterns = [
            re.compile(
                r'https://[A-Za-z0-9.-]+\.buzzheavier\.com/d/[^\s"\'<>]+',
                re.IGNORECASE,
            ),
            re.compile(
                r'https://[A-Za-z0-9.-]+\.bzzhr\.to/d/[^\s"\'<>]+',
                re.IGNORECASE,
            ),
        ]

        for pattern in body_patterns:
            match = pattern.search(body)
            if not match:
                continue

            candidate = (
                match.group(0)
                .replace("&amp;", "&")
                .rstrip(".,;)")
            )

            if is_direct_download_url(candidate):
                return candidate, "body"

        # ----------------------------------------------------
        # Diagnóstico: saber si recibimos publicidad/redirección
        # ----------------------------------------------------
        if location:
            print(
                "      ℹ El primer intento devolvió un Location "
                "que no es el enlace del archivo. Se reintentará."
            )

        return None, "sin-enlace-directo"

    except Exception as exc:
        print(
            f"      ✗ Error solicitando /download: "
            f"{type(exc).__name__}: {exc}"
        )
        return None, "error-http"


# ============================================================
# BUZZHEAVIER - FLUJO COMPLETO DE RESOLUCIÓN
# ============================================================

def get_buzzheavier_direct_url(file_id: str) -> str | None:
    """
    Obtiene el verdadero enlace firmado de BuzzHeavier.

    Se utiliza UNA MISMA sesión HTTP durante todo el proceso,
    de modo que cookies/estado obtenidos durante el primer
    intento permanezcan disponibles para el segundo.

    Esto reproduce el comportamiento observado por el usuario:
    primer intento -> posible publicidad/estado -> segundo
    intento -> enlace directo.
    """

    if not file_id:
        print("    ✗ file_id vacío.")
        return None

    # Para curl_cffi necesitamos crear una sesión de curl_cffi
    try:
        session = curl_requests.Session(impersonate=IMPERSONATE_TARGET)
    except Exception:
        session = requests.Session()

    try:
        # ----------------------------------------------------
        # La sesión mantiene cookies igual que un navegador.
        # ----------------------------------------------------
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
            }
        )

        page_result = get_file_page_and_endpoints(
            file_id,
            session,
        )

        if not page_result:
            return None

        page_url, endpoints = page_result

        # Intentamos los endpoints encontrados. La página puede
        # tener Server 1 y Server 2.
        for server_index, endpoint in enumerate(
            endpoints,
            start=1,
        ):

            print(
                f"    → Resolviendo servidor S{server_index}..."
            )

            # ------------------------------------------------
            # Repetimos el mismo GET varias veces.
            # No seguimos redirects de publicidad.
            # ------------------------------------------------
            for attempt in range(
                1,
                DIRECT_LINK_ATTEMPTS + 1,
            ):

                print(
                    f"      Intento {attempt}/{DIRECT_LINK_ATTEMPTS}..."
                )

                direct_url, source = request_direct_link(
                    endpoint,
                    page_url,
                    session,
                )

                if direct_url:
                    print(
                        "    ✓ ENLACE DIRECTO REAL OBTENIDO"
                    )
                    print(
                        f"      Método: {source}"
                    )
                    print(
                        f"      URL: {direct_url}"
                    )

                    return direct_url

                if attempt < DIRECT_LINK_ATTEMPTS:
                    time.sleep(DIRECT_LINK_RETRY_DELAY)

            print(
                f"    ⚠ S{server_index} no entregó un enlace directo."
            )

        print(
            "    ✗ Ningún servidor de BuzzHeavier entregó "
            "el enlace directo firmado."
        )

        return None

    finally:
        try:
            session.close()
        except Exception:
            pass


# ============================================================
# SUBIDA A BUZZHEAVIER
# ============================================================

def upload_to_buzzheavier(local_path: Path) -> str | None:
    filename = local_path.name
    encoded_name = quote(
        filename,
        safe="",
    )

    upload_url = (
        f"https://w.buzzheavier.com/{encoded_name}"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Android) ManhuasApp/1.0",
    }

    if BUZZHEAVIER_ACCOUNT_ID:
        headers["Authorization"] = (
            f"Bearer {BUZZHEAVIER_ACCOUNT_ID}"
        )

    # Para la subida usamos requests normal (funciona bien)
    session = requests.Session()
    session.headers.update(headers)

    try:
        size_mb = (
            local_path.stat().st_size
            / (1024 * 1024)
        )

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
            f"    → Respuesta de subida: "
            f"HTTP {response.status_code}"
        )

        if response.status_code not in (200, 201):
            print(
                f"    ✗ Error HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
            return None

        file_id = None
        response_direct_url = None

        # ----------------------------------------------------
        # Analizar JSON de BuzzHeavier
        # ----------------------------------------------------
        try:
            response_data = response.json()
            print(
                f"    Respuesta de subida: {response_data}"
            )

            inner = response_data

            if (
                isinstance(response_data, dict)
                and isinstance(response_data.get("data"), dict)
            ):
                inner = response_data["data"]

            if isinstance(inner, dict):
                file_id = (
                    inner.get("id")
                    or inner.get("fileId")
                    or inner.get("file_id")
                )

                response_direct_url = (
                    inner.get("downloadUrl")
                    or inner.get("download_url")
                    or inner.get("url")
                    or inner.get("link")
                )

            if (
                not file_id
                and isinstance(response_data, dict)
            ):
                file_id = response_data.get("id")

        except json.JSONDecodeError:
            text = response.text.strip()
            print(
                f"    Respuesta no JSON: {text[:500]}"
            )

            if text.startswith("http"):
                response_direct_url = text

        # ----------------------------------------------------
        # Si API ya dio enlace directo válido, usarlo.
        # ----------------------------------------------------
        if response_direct_url:
            response_direct_url = normalize_url(
                response_direct_url,
                upload_url,
            )

            if response_direct_url and is_direct_download_url(
                response_direct_url
            ):
                print(
                    "    ✓ La API ya entregó un enlace directo válido."
                )
                print(
                    f"      {response_direct_url}"
                )
                return response_direct_url

            print(
                "    ⚠ La URL entregada directamente por la API "
                "no parece ser el enlace firmado final."
            )

        if not file_id:
            print(
                "    ✗ No se encontró file_id en la respuesta de subida."
            )
            return None

        print(
            f"    ✓ ID de archivo obtenido: {file_id}"
        )

        # ----------------------------------------------------
        # Resolver el enlace real desde la página pública.
        # ----------------------------------------------------
        return get_buzzheavier_direct_url(
            str(file_id)
        )

    except requests.RequestException as exc:
        print(
            f"    ✗ Error HTTP subiendo archivo: "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    except Exception as exc:
        print(
            f"    ✗ Error durante la subida: "
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

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION_STR),
        API_ID,
        API_HASH,
    )

    print("Conectando con Telegram...")

    await client.start()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "La sesión de Telegram no está autorizada."
        )

    me = await client.get_me()

    print(
        f"✓ Conectado como: {me.first_name} (@{me.username})"
    )

    catalog = load_catalog()
    pending = find_packages_without_url(catalog)
    total = len(pending)

    print(
        f"Paquetes sin URL detectados: {total}"
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

    for idx, (series, pack) in enumerate(
        to_process,
        start=1,
    ):

        series_name = series.get(
            "name",
            "Desconocida",
        )

        series_id = series.get(
            "id",
            "",
        )

        pack_name = pack.get(
            "name",
            "Paquete",
        )

        message_id = pack.get("message_id")

        filename = pack.get(
            "filename",
            f"{message_id}.pdf",
        )

        is_visible = series.get(
            "visible",
            True,
        )

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

        # ----------------------------------------------------
        # Descargar desde Telegram
        # ----------------------------------------------------
        try:
            print("    ↓ Descargando de Telegram...")

            if not message_id:
                raise ValueError(
                    "El paquete no tiene message_id."
                )

            message = await client.get_messages(
                MAIN_CHANNEL,
                ids=message_id,
            )

            if not message or not message.file:
                raise ValueError(
                    f"Mensaje {message_id} sin archivo."
                )

            await client.download_media(
                message,
                file=str(local_file),
            )

            if (
                not local_file.exists()
                or local_file.stat().st_size == 0
            ):
                raise ValueError(
                    "Archivo vacío."
                )

            size_mb = (
                local_file.stat().st_size
                / (1024 * 1024)
            )

            print(
                f"    ✓ Descargado: {size_mb:.1f} MB"
            )

        except Exception as exc:
            print(
                f"    ✗ Error descargando: {exc}"
            )

            series_stats[series_id]["fail"] += 1
            fail_count += 1

            if local_file.exists():
                try:
                    local_file.unlink()
                except OSError:
                    pass

            continue

        # ----------------------------------------------------
        # Subir y obtener URL directa
        # ----------------------------------------------------
        download_url = upload_to_buzzheavier(
            local_file
        )

        if download_url:
            pack["download_url"] = download_url

            series_stats[series_id]["success"] += 1
            success_count += 1

            print(
                "    ✓ PAQUETE COMPLETADO CON ENLACE DIRECTO"
            )
        else:
            series_stats[series_id]["fail"] += 1
            fail_count += 1

            print(
                "    ✗ PAQUETE FALLIDO: NO SE GUARDÓ UNA URL FALSA"
            )

        # ----------------------------------------------------
        # Borrar temporal
        # ----------------------------------------------------
        try:
            if local_file.exists():
                local_file.unlink()
        except OSError:
            pass

    # --------------------------------------------------------
    # Guardar catálogo
    # --------------------------------------------------------
    save_catalog_output(catalog)

    remaining = total - len(to_process)

    report_lines = [
        "📊 *Uploader BuzzHeavier — Reporte*",
        (
            "Fecha: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M UTC')}"
        ),
        "",
        f"✅ Con enlace directo: {success_count}",
        f"❌ Fallidos: {fail_count}",
        f"📦 Procesados: {len(to_process)} de {total}",
    ]

    if remaining > 0:
        report_lines.append(
            "⏳ Pendientes para la próxima ejecución: "
            f"{remaining}"
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

    # --------------------------------------------------------
    # Reporte
    # --------------------------------------------------------
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
                "📎 Catálogo con URLs directas actualizadas."
            ),
        )

        print("✓ Catálogo enviado.")

    except Exception as exc:
        print(
            f"⚠ Error al enviar reporte: {exc}"
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
