"""Подпись presigned URL по AWS Signature Version 4 (S3, путь вида /bucket/key).

Минимальная реализация для тестов стенда: приложение подписывает ссылки в S5 (aiobotocore), а тесты
S4 проверяют, что такие ссылки проходят через Caddy и SeaweedFS. Свой код здесь, чтобы не тянуть
зависимость, которая понадобится приложению только в S5. Реализацию сверяет пример из документации
AWS (test_sigv4_signer.py).
"""

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def presign_url(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    expires: int = 600,
    signed_headers: dict[str, str] | None = None,
    region: str = "us-east-1",
    now: datetime | None = None,
) -> str:
    """Возвращает `url` с параметрами X-Amz-*; `signed_headers` клиент обязан отправить как есть."""
    parts = urlsplit(url)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"

    headers = {
        "host": parts.netloc,
        **{k.lower(): v.strip() for k, v in (signed_headers or {}).items()},
    }
    names = ";".join(sorted(headers))
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": names,
    }
    canonical_query = "&".join(
        f"{quote(key, safe='~')}={quote(value, safe='~')}" for key, value in sorted(query.items())
    )
    canonical_uri = quote(parts.path or "/", safe="/~")
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, names, UNSIGNED_PAYLOAD]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    key = _hmac(f"AWS4{secret_key}".encode(), amz_date[:8])
    for part in (region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{parts.netloc}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"
