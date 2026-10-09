# refresh_urls_idrive.py
# Regenera TODAS las URLs prefirmadas del catálogo (para evitar que expiren).
# Se ejecuta desde GitHub Actions cada 4 días.

import json
import os
from pathlib import Path
from urllib.parse import urlparse, unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


BASE_DIR = Path(__file__).resolve().parent
CATALOG_FILE = BASE_DIR / "catalog.json"

IDRIVE_ENDPOINT = os.getenv("IDRIVE_ENDPOINT")
IDRIVE_ACCESS_KEY = os.getenv("IDRIVE_ACCESS_KEY")
IDRIVE_SECRET_KEY = os.getenv("IDRIVE_SECRET_KEY")
IDRIVE_BUCKET = os.getenv("IDRIVE_BUCKET")
IDRIVE_REGION = os.getenv("IDRIVE_REGION", "us-southeast-1")

PRESIGN_EXPIRATION = 604800  # 7 días


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=IDRIVE_ENDPOINT,
        aws_access_key_id=IDRIVE_ACCESS_KEY,
        aws_secret_access_key=IDRIVE_SECRET_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        region_name=IDRIVE_REGION,
    )


def extraer_key_desde_url(url: str, bucket: str) -> str | None:
    """
    Extrae el key S3 desde una URL prefirmada antigua.

    IMPORTANTE: se hace unquote() del path para des-encodear %20, %C3%B3, etc.
    Sin esto, boto3 re-encodea el '%' como '%25' y firma una key inexistente.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path).lstrip("/")
        if path.startswith(bucket + "/"):
            path = path[len(bucket) + 1:]
        return path
    except Exception:
        return None


def main():
    if not CATALOG_FILE.exists():
        print("✗ No existe catalog.json")
        return

    catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    print("→ Catálogo cargado")

    s3 = get_s3_client()

    total = 0
    regeneradas = 0
    errores = 0

    for list_key in ("series", "novels"):
        for series in catalog.get(list_key, []):
            series_id = series.get("id", "?")
            for pack in series.get("packages", []):
                url_actual = pack.get("download_url")
                if not url_actual:
                    continue

                total += 1
                message_id = pack.get("message_id", "?")

                key = extraer_key_desde_url(url_actual, IDRIVE_BUCKET)
                if not key:
                    print(f"   ⚠ [{series_id}] msg {message_id}: no se pudo extraer key de {url_actual[:80]}")
                    errores += 1
                    continue

                try:
                    nueva_url = s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": IDRIVE_BUCKET, "Key": key},
                        ExpiresIn=PRESIGN_EXPIRATION,
                    )
                    pack["download_url"] = nueva_url
                    regeneradas += 1
                except ClientError as e:
                    print(f"   ✗ [{series_id}] msg {message_id}: {e}")
                    errores += 1

    CATALOG_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n✓ Total URLs: {total}")
    print(f"✓ Regeneradas: {regeneradas}")
    print(f"✗ Errores: {errores}")


if __name__ == "__main__":
    main()
