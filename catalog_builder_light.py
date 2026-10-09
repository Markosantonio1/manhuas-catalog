# catalog_builder_light.py
# Builder LIGERO de catálogo para GitHub Actions.
#
# Toma el catalog.json existente del repo y SOLO AÑADE capítulos/volúmenes
# nuevos detectados en Telegram. NO descarga portadas. NO reescribe la
# estructura. NO toca las series existentes más allá de añadir packages.
#
# - Manhuas: añade packages SIN download_url (los sube el uploader).
# - Novelas: añade packages descargando el PDF y subiéndolo a GitHub.

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURACIÓN
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CATALOG_FILE = BASE_DIR / "catalog.json"

MAIN_CHANNEL_ID = -1003003829878
MAIN_CHANNEL_USERNAME = "manhuasgratis"
NOVELS_CHANNEL_ID = -1004334091126

GITHUB_USER = "Markosantonio1"
GITHUB_REPO = "manhuas-catalog"
GITHUB_BRANCH = "main"
NOVELS_REPO_DIR = "novels"

SCAN_WAIT = 0.15
MAX_CHAPTER = 5000

# Cuántos mensajes de margen hacia atrás escanear (por si hay albums partidos)
SCAN_MARGIN = 100

TRIGGER_UPLOADER = os.getenv("TRIGGER_UPLOADER", "1") == "1"
MAX_PACKAGES_UPLOAD = int(os.getenv("MAX_PACKAGES_UPLOAD", "100"))


# ============================================================
# ENV
# ============================================================

load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
SESSION_STR = os.getenv("TELEGRAM_SESSION_STR", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()


# ============================================================
# UTILIDADES (copiadas de todo.py)
# ============================================================

def normalize_hashtag(value: str) -> str:
    value = unicodedata.normalize("NFC", (value or "").strip())
    if not value:
        return ""
    if not value.startswith("#"):
        value = "#" + value
    value = value.replace(" ", "_")
    value = re.sub(r"\s+", "_", value)
    value = "#" + value.lstrip("#")
    return value


def hashtag_key(value: str) -> str:
    return normalize_hashtag(value).casefold()


def extract_hashtags(text: str) -> list:
    if not text:
        return []
    matches = re.findall(
        r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", text, flags=re.UNICODE,
    )
    result, seen = [], set()
    for match in matches:
        tag = normalize_hashtag(match)
        key = hashtag_key(tag)
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def sanitize_filename(value: str) -> str:
    """⚠ IDÉNTICA a uploader_idrive.py"""
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", value or "")
    cleaned = re.sub(r"[\x00-\x1f]", "_", cleaned)
    cleaned = cleaned.strip()
    return cleaned or "archivo.pdf"


def normalize_filename(filename: str) -> str:
    text = unicodedata.normalize("NFKC", filename or "")
    for dash in "‐-‒–—―−":
        text = text.replace(dash, "-")
    return text


def valid_chapter(value: int) -> bool:
    return 0 <= value <= MAX_CHAPTER


def likely_year(value: int) -> bool:
    return 1900 <= value <= 2100


def extract_chapter_range(filename: str) -> Optional[tuple]:
    text = normalize_filename(filename)
    text = re.sub(r"\.[^.]+$", "", text)

    candidates = []
    for match in re.finditer(
        r"(?<!\d)(\d{1,4})\s*[-_~]\s*(\d{1,4})(?!\d)", text,
    ):
        first, last = int(match.group(1)), int(match.group(2))
        if not valid_chapter(first) or not valid_chapter(last):
            continue
        if last < first:
            continue
        if likely_year(first) and likely_year(last):
            continue
        if last - first + 1 > MAX_CHAPTER:
            continue
        start = match.start()
        prefix = text[max(0, start - 15):start]
        score = 100
        if start < 18: score += 30
        elif start < 50: score += 15
        if "#" in prefix: score += 10
        if any(s in prefix for s in ("[", "(", "{", "〖", "〗", "【", "】")):
            score += 15
        candidates.append((score, start, first, last))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][2], candidates[0][3]

    candidates = []
    for match in re.finditer(r"(?<![\d-])(\d{1,4})(?![\d-])", text):
        value = int(match.group(1))
        if not valid_chapter(value) or likely_year(value):
            continue
        start = match.start()
        prefix = text[max(0, start - 15):start]
        score = 20
        if start < 10: score += 35
        elif start < 30: score += 20
        elif start < 60: score += 5
        if "#" in prefix: score += 15
        if any(s in prefix for s in ("[", "(", "{", "〖", "〗", "【", "】")):
            score += 20
        if value <= 999: score += 10
        candidates.append((score, start, value))

    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][2], candidates[0][2]

    return None


def is_pdf_message(message) -> bool:
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return False
    mime = (getattr(file_obj, "mime_type", None) or "").lower()
    name = (getattr(file_obj, "name", None) or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf")


def telegram_link(channel_id: int, message_id: int, username: Optional[str]) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{channel_id}/{message_id}"


def detect_volume_number(filename: str, default: int) -> int:
    if not filename:
        return default
    name = Path(filename).stem
    match = re.search(
        r"\b(?:vol(?:umen)?|tomo|book|libro|v)[\s_.\-]*(\d{1,3})\b",
        name, flags=re.IGNORECASE,
    )
    if match:
        return int(match.group(1))
    return default


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "serie"


# ============================================================
# GITHUB — SUBIR NOVELAS (blob)
# ============================================================

def _make_github_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ManhuasBot-Light/1.0",
    })
    return session


async def upload_novel_to_github(series_id: str, local_pdf: Path) -> Optional[str]:
    if not GITHUB_TOKEN:
        print("  ⚠ GITHUB_TOKEN no configurado")
        return None

    api_base = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
    session = _make_github_session(GITHUB_TOKEN)

    try:
        ref = session.get(f"{api_base}/git/ref/heads/{GITHUB_BRANCH}", timeout=30)
        ref.raise_for_status()
        parent_commit_sha = ref.json()["object"]["sha"]

        commit = session.get(f"{api_base}/git/commits/{parent_commit_sha}", timeout=30)
        commit.raise_for_status()
        base_tree_sha = commit.json()["tree"]["sha"]

        content_b64 = base64.b64encode(local_pdf.read_bytes()).decode("utf-8")
        blob = session.post(
            f"{api_base}/git/blobs",
            json={"content": content_b64, "encoding": "base64"},
            timeout=600,
        )
        blob.raise_for_status()
        blob_sha = blob.json()["sha"]

        path_in_repo = f"{NOVELS_REPO_DIR}/{series_id}/{local_pdf.name}"
        tree = session.post(
            f"{api_base}/git/trees",
            json={
                "base_tree": base_tree_sha,
                "tree": [{
                    "path": path_in_repo,
                    "mode": "100644", "type": "blob", "sha": blob_sha,
                }],
            },
            timeout=60,
        )
        tree.raise_for_status()
        new_tree_sha = tree.json()["sha"]

        new_commit = session.post(
            f"{api_base}/git/commits",
            json={
                "message": f"Novela: {series_id} - {local_pdf.name}",
                "tree": new_tree_sha,
                "parents": [parent_commit_sha],
            },
            timeout=30,
        )
        new_commit.raise_for_status()
        new_commit_sha = new_commit.json()["sha"]

        update = session.patch(
            f"{api_base}/git/refs/heads/{GITHUB_BRANCH}",
            json={"sha": new_commit_sha, "force": False},
            timeout=30,
        )
        update.raise_for_status()

        return (
            f"https://raw.githubusercontent.com/"
            f"{GITHUB_USER}/{GITHUB_REPO}/main/{path_in_repo}"
        )

    except requests.exceptions.RequestException as exc:
        print(f"    ✗ Error subiendo novela: {exc}")
        return None
    finally:
        session.close()


# ============================================================
# CATÁLOGO
# ============================================================

def load_catalog() -> dict:
    if not CATALOG_FILE.exists():
        print(f"✗ No existe {CATALOG_FILE}")
        sys.exit(1)
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def save_catalog(catalog: dict) -> None:
    CATALOG_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ Catálogo guardado ({CATALOG_FILE.stat().st_size} bytes)")


def compute_min_message_id(items: list) -> int:
    max_id = 0
    for item in items:
        for p in item.get("packages", []):
            mid = p.get("message_id")
            if isinstance(mid, int) and mid > max_id:
                max_id = mid
    return max(0, max_id - SCAN_MARGIN)


# ============================================================
# ESCANEO — solo mensajes nuevos
# ============================================================

def build_series_key_map(catalog: dict) -> dict:
    """Devuelve {hashtag_key: series_dict} para manhuas y novelas."""
    result = {"series": {}, "novels": {}}
    for lst_key in ("series", "novels"):
        for item in catalog.get(lst_key, []):
            h = item.get("hashtag")
            if h:
                result[lst_key][hashtag_key(h)] = item
    return result


async def scan_main_channel_new(client, selected_keys: set, min_id: int) -> dict:
    """Escanea el canal principal y devuelve {hashtag_key: [msg, ...]} solo con
    mensajes NO existentes en el catálogo."""
    result = defaultdict(list)
    processed_groups = set()

    messages = []
    grouped = defaultdict(list)

    print(f"  → Iterando canal principal desde msg_id > {min_id}...")
    count = 0
    async for message in client.iter_messages(
        MAIN_CHANNEL_ID,
        limit=None,
        min_id=min_id,
        wait_time=SCAN_WAIT,
    ):
        count += 1
        messages.append(message)
        gid = getattr(message, "grouped_id", None)
        if gid:
            grouped[int(gid)].append(message)
    print(f"  → {count} mensajes nuevos leídos")

    # 1) Albums con hashtag
    for gid, members in grouped.items():
        pdf_members = [m for m in members if is_pdf_message(m)]
        if not pdf_members:
            continue
        group_tags = set()
        for m in members:
            for tag in extract_hashtags(getattr(m, "raw_text", "") or ""):
                k = hashtag_key(tag)
                if k in selected_keys:
                    group_tags.add(k)
        if group_tags:
            processed_groups.add(gid)
            for m in pdf_members:
                for k in group_tags:
                    result[k].append(m)

    # 2) PDFs individuales con hashtag
    for m in messages:
        if not is_pdf_message(m):
            continue
        gid = getattr(m, "grouped_id", None)
        if gid and int(gid) in processed_groups:
            continue
        for tag in extract_hashtags(getattr(m, "raw_text", "") or ""):
            k = hashtag_key(tag)
            if k in selected_keys:
                result[k].append(m)

    return result


async def scan_novels_channel_new(client, selected_keys: set, min_id: int) -> dict:
    """Igual pero para el canal de novelas."""
    result = defaultdict(list)
    messages = []

    print(f"  → Iterando canal de novelas desde msg_id > {min_id}...")
    async for msg in client.iter_messages(
        NOVELS_CHANNEL_ID,
        limit=None,
        min_id=min_id,
        wait_time=SCAN_WAIT,
    ):
        messages.append(msg)
    print(f"  → {len(messages)} mensajes nuevos leídos")

    for msg in messages:
        if not is_pdf_message(msg):
            continue
        tags = extract_hashtags(getattr(msg, "raw_text", "") or "")
        matching = [hashtag_key(t) for t in tags if hashtag_key(t) in selected_keys]
        for k in matching:
            result[k].append(msg)

    return result


# ============================================================
# CONSTRUIR NUEVOS PACKAGES
# ============================================================

def build_manhua_package(msg) -> Optional[dict]:
    message_id = int(msg.id)
    filename = (getattr(msg.file, "name", None) or f"{message_id}.pdf").strip()
    chapter_range = extract_chapter_range(filename)
    if chapter_range is None:
        print(f"    ⚠ Ignorado (sin capítulo): {filename}")
        return None
    first, last = chapter_range
    return {
        "name": str(first) if first == last else f"{first}-{last}",
        "first": first,
        "last": last,
        "channel_id": MAIN_CHANNEL_ID,
        "message_id": message_id,
        "filename": filename,
        "size": int(getattr(msg.file, "size", 0) or 0),
        "date": msg.date.isoformat() if getattr(msg, "date", None) else None,
        "telegram_link": telegram_link(MAIN_CHANNEL_ID, message_id, MAIN_CHANNEL_USERNAME),
        "download_ready": True,
        "chapters": list(range(first, last + 1)),
    }


async def build_novel_package(client, novel: dict, msg) -> Optional[dict]:
    message_id = int(msg.id)
    filename = (getattr(msg.file, "name", None) or f"volumen_{message_id}.pdf").strip()
    size = int(getattr(msg.file, "size", 0) or 0)
    date = msg.date.isoformat() if getattr(msg, "date", None) else None
    series_id = novel.get("id") or slugify(novel.get("name", "novela"))

    temp_dir = BASE_DIR / "novels_cache" / series_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_path = temp_dir / sanitize_filename(filename)

    print(f"    ↓ Descargando volumen: {filename}")
    try:
        await client.download_media(msg, file=str(local_path))
    except Exception as exc:
        print(f"    ✗ Error descargando: {exc}")
        return None

    if not local_path.exists() or local_path.stat().st_size == 0:
        print("    ✗ Descarga vacía")
        return None

    print(f"    ↑ Subiendo a GitHub: {filename}")
    url = await upload_novel_to_github(series_id, local_path)

    try:
        local_path.unlink()
    except Exception:
        pass

    if not url:
        print("    ✗ No se pudo subir a GitHub")
        return None

    volume_number = detect_volume_number(filename, message_id)
    return {
        "name": f"Volumen {volume_number}",
        "first": volume_number,
        "last": volume_number,
        "channel_id": NOVELS_CHANNEL_ID,
        "message_id": message_id,
        "filename": filename,
        "size": size,
        "date": date,
        "telegram_link": telegram_link(NOVELS_CHANNEL_ID, message_id, None),
        "download_ready": True,
        "chapters": [volume_number],
        "download_url": url,
        "is_novel_volume": True,
    }


# ============================================================
# GIT + DISPATCH UPLOADER
# ============================================================

def git_commit_and_push(mensaje: str) -> bool:
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
        subprocess.run(["git", "add", "catalog.json"],
                       check=False, capture_output=True)
        c = subprocess.run(
            ["git", "commit", "-m", mensaje, "--allow-empty"],
            check=False, capture_output=True, text=True,
        )
        if c.returncode != 0:
            print(f"  ⚠ commit rc={c.returncode}: {c.stdout[:200]}")

        p = subprocess.run(
            ["git", "push"],
            check=False, capture_output=True, text=True,
        )
        if p.returncode != 0:
            print(f"  ⚠ push rc={p.returncode}: {p.stderr[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"  ⚠ Error commit/push: {exc}")
        return False


def trigger_uploader_workflow() -> bool:
    if not GITHUB_TOKEN:
        print("  ⚠ Sin GITHUB_TOKEN, no se dispara uploader")
        return False

    url = (
        f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
        f"/actions/workflows/uploader_idrive.yml/dispatches"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {"max_packages": str(MAX_PACKAGES_UPLOAD)},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 204:
            print(f"  ✓ Uploader disparado (max_packages={MAX_PACKAGES_UPLOAD})")
            return True
        print(f"  ✗ HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as exc:
        print(f"  ✗ Error disparando uploader: {exc}")
        return False


def set_github_output(name: str, value: str) -> None:
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


# ============================================================
# MAIN
# ============================================================

async def main():
    print("=" * 60)
    print("CATALOG BUILDER LIGHT")
    print("=" * 60)

    if not API_ID or not API_HASH or not SESSION_STR:
        print("✗ Faltan credenciales de Telegram")
        sys.exit(1)

    catalog = load_catalog()
    print(f"✓ Catálogo cargado: {len(catalog.get('series', []))} series, "
          f"{len(catalog.get('novels', []))} novelas")

    # Mapa hashtag_key → series/novel
    key_map = build_series_key_map(catalog)
    manhua_keys = set(key_map["series"].keys())
    novel_keys = set(key_map["novels"].keys())

    print(f"  → Manhuas a vigilar: {len(manhua_keys)}")
    print(f"  → Novelas a vigilar: {len(novel_keys)}")

    if not manhua_keys and not novel_keys:
        print("Nada que escanear. Saliendo.")
        set_github_output("changed", "false")
        set_github_output("changes", "0")
        return

    # Cliente Telegram
    client = TelegramClient(
        StringSession(SESSION_STR),
        API_ID, API_HASH,
        connection_retries=5, retry_delay=3, timeout=60, request_retries=5,
    )
    await client.start()
    if not await client.is_user_authorized():
        print("✗ Sesión de Telegram no autorizada")
        sys.exit(1)
    me = await client.get_me()
    print(f"✓ Conectado como {me.first_name} (@{me.username})")

    changes = 0
    changes_manhua = 0
    changes_novel = 0

    try:
        # ---- MANHUAS ----
        if manhua_keys:
            min_id = compute_min_message_id(catalog.get("series", []))
            print(f"\n--- Canal principal (manhuas) ---")
            new_msgs = await scan_main_channel_new(client, manhua_keys, min_id)

            for hkey, series in key_map["series"].items():
                existing = {
                    p.get("message_id")
                    for p in series.get("packages", [])
                    if isinstance(p.get("message_id"), int)
                }
                for msg in new_msgs.get(hkey, []):
                    if int(msg.id) in existing:
                        continue
                    pkg = build_manhua_package(msg)
                    if pkg:
                        series.setdefault("packages", []).append(pkg)
                        existing.add(pkg["message_id"])
                        changes_manhua += 1
                        changes += 1
                        print(f"  + {series.get('name')} → {pkg['name']}")

                if changes_manhua > 0:
                    series["packages"].sort(
                        key=lambda p: (p["first"], p["last"], p["message_id"])
                    )
                    series["package_count"] = len(series["packages"])
                    series["latest_chapter"] = max(
                        (p["last"] for p in series["packages"]), default=0,
                    )

        # ---- NOVELAS ----
        if novel_keys:
            min_id = compute_min_message_id(catalog.get("novels", []))
            print(f"\n--- Canal de novelas ---")
            new_msgs = await scan_novels_channel_new(client, novel_keys, min_id)

            for hkey, novel in key_map["novels"].items():
                existing = {
                    p.get("message_id")
                    for p in novel.get("packages", [])
                    if isinstance(p.get("message_id"), int)
                }
                for msg in new_msgs.get(hkey, []):
                    if int(msg.id) in existing:
                        continue
                    pkg = await build_novel_package(client, novel, msg)
                    if pkg:
                        novel.setdefault("packages", []).append(pkg)
                        existing.add(pkg["message_id"])
                        changes_novel += 1
                        changes += 1
                        print(f"  + {novel.get('name')} → {pkg['name']}")

                if changes_novel > 0:
                    novel["packages"].sort(
                        key=lambda p: (p["first"], p["last"], p["message_id"])
                    )
                    novel["package_count"] = len(novel["packages"])
                    novel["latest_chapter"] = max(
                        (p["last"] for p in novel["packages"]), default=0,
                    )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # ---- RESULTADO ----
    print()
    print("=" * 60)
    print(f"Capítulos nuevos (manhuas): {changes_manhua}")
    print(f"Volúmenes nuevos (novelas): {changes_novel}")
    print(f"TOTAL cambios: {changes}")
    print("=" * 60)

    if changes == 0:
        print("Sin cambios. No se hace commit ni dispatch.")
        set_github_output("changed", "false")
        set_github_output("changes", "0")
        return

    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_catalog(catalog)

    if git_commit_and_push(
        f"Catálogo: +{changes} capítulos/volúmenes nuevos"
    ):
        print("✓ Commit subido a GitHub")
    else:
        print("⚠ No se pudo subir el commit")

    set_github_output("changed", "true")
    set_github_output("changes", str(changes))

    if TRIGGER_UPLOADER:
        print("\n--- Disparando uploader ---")
        trigger_uploader_workflow()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelado.")
    except Exception as exc:
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}")
        sys.exit(1)
