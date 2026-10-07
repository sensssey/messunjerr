"""Подписыватель SigV4 сверен с примером из документации AWS (стенд не нужен)."""

from datetime import UTC, datetime

import pytest

from .sigv4 import presign_url

pytestmark = pytest.mark.offline


def test_matches_the_presigned_get_example_from_the_aws_documentation() -> None:
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html
    url = presign_url(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        expires=86400,
        now=datetime(2013, 5, 24, 0, 0, 0, tzinfo=UTC),
    )
    assert url.endswith(
        "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url
    assert "X-Amz-SignedHeaders=host" in url


def test_signed_headers_are_listed_sorted_and_lowercase() -> None:
    url = presign_url(
        "PUT",
        "https://messunjerr.localhost/media/uploads/a.webp",
        access_key="AK",
        secret_key="secret",
        signed_headers={"Content-Type": "image/webp", "Content-Length": "10"},
    )
    assert "X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost" in url


def test_key_with_spaces_and_cyrillic_is_percent_encoded_once() -> None:
    url = presign_url(
        "GET",
        "https://messunjerr.localhost/media/uploads/мой файл.txt",
        access_key="AK",
        secret_key="secret",
    )
    assert "/media/uploads/%D0%BC%D0%BE%D0%B9%20%D1%84%D0%B0%D0%B9%D0%BB.txt?" in url
