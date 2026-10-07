"""Ошибки медиа по каталогу 5.14: коды верхнего уровня и элементы `errors[]` у `validation_error`."""

from messunjerr.core.codes import ErrorCode, ItemCode
from messunjerr.core.errors import DomainError, ErrorItem
from messunjerr.media.domain.rules import RejectReason


def quota_exceeded(*, limit: int, used: int) -> DomainError:
    return DomainError(
        ErrorCode.QUOTA_EXCEEDED,
        "The storage quota is exhausted: delete files you do not need.",
        limit=limit,
        used=used,
    )


def upload_missing() -> DomainError:
    return DomainError(
        ErrorCode.UPLOAD_MISSING,
        "The object is not in the storage yet: upload the file to the presigned URL first.",
    )


_REJECT_DETAILS: dict[RejectReason, str] = {
    RejectReason.SIZE_MISMATCH: "The uploaded size differs from the declared one.",
    RejectReason.SIZE_EXCEEDS_LIMIT: "The uploaded file is larger than allowed.",
}


def upload_rejected(reason: RejectReason) -> DomainError:
    return DomainError(
        ErrorCode.UPLOAD_REJECTED,
        _REJECT_DETAILS.get(reason, "The upload was rejected."),
        reason=reason.value,
    )


def asset_in_use() -> DomainError:
    return DomainError(
        ErrorCode.ASSET_IN_USE,
        "The asset is attached to a profile, post or message: detach it first.",
    )


# --- элементы errors[] у POST /media/uploads (5.8)
def purpose_invalid(allowed: tuple[str, ...]) -> ErrorItem:
    return ErrorItem(
        "/body/purpose",
        ItemCode.PURPOSE_INVALID,
        "Use one of: " + ", ".join(allowed) + ".",
        {"allowed": ", ".join(allowed)},
    )


def content_type_not_allowed(allowed: tuple[str, ...] | None = None) -> ErrorItem:
    if allowed is None:
        return ErrorItem(
            "/body/content_type",
            ItemCode.CONTENT_TYPE_NOT_ALLOWED,
            "Use a media type of the form type/subtype.",
        )
    return ErrorItem(
        "/body/content_type",
        ItemCode.CONTENT_TYPE_NOT_ALLOWED,
        "This purpose accepts only: " + ", ".join(allowed) + ".",
        {"allowed": ", ".join(allowed)},
    )


def extension_forbidden(extension: str) -> ErrorItem:
    return ErrorItem(
        "/body/filename",
        ItemCode.EXTENSION_FORBIDDEN,
        "Files with this extension cannot be uploaded.",
        {"extension": extension},
    )


def size_invalid() -> ErrorItem:
    return ErrorItem("/body/size_bytes", ItemCode.SIZE_INVALID, "The size must be positive.")


def size_exceeds_limit(max_bytes: int) -> ErrorItem:
    return ErrorItem(
        "/body/size_bytes",
        ItemCode.SIZE_EXCEEDS_LIMIT,
        "The file is larger than allowed for this purpose.",
        {"max_bytes": max_bytes},
    )
