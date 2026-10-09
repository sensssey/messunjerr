"""Ошибки социального графа из каталога 5.14. Ресурс, которого зритель видеть не должен, это
`NotFoundError` из ядра (`404`): наружу «нет» и «скрыто» неразличимы (4.6)."""

from messunjerr.core.codes import ErrorCode
from messunjerr.core.errors import DomainError


def self_action() -> DomainError:
    return DomainError(ErrorCode.SELF_ACTION, "This action cannot be applied to yourself.")


def already_friends() -> DomainError:
    return DomainError(ErrorCode.ALREADY_FRIENDS, "You are already friends.")


def friend_request_exists() -> DomainError:
    return DomainError(
        ErrorCode.FRIEND_REQUEST_EXISTS, "Your friend request to this person is already waiting."
    )


def friend_request_not_pending() -> DomainError:
    return DomainError(
        ErrorCode.FRIEND_REQUEST_NOT_PENDING, "The friend request has already been answered."
    )


def follow_request_not_pending() -> DomainError:
    return DomainError(
        ErrorCode.FOLLOW_REQUEST_NOT_PENDING, "The follow request has already been answered."
    )


def list_hidden() -> DomainError:
    return DomainError(ErrorCode.LIST_HIDDEN, "The owner does not show this list to you.")


def profile_private() -> DomainError:
    return DomainError(
        ErrorCode.PROFILE_PRIVATE, "The profile is private: befriend or follow this person first."
    )
