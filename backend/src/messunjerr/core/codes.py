"""Коды ошибок API: единый источник для problem+json (каталог 5.14 backend-v2-spec.md).

Файл сгенерирован `scripts/gen_error_codes.py`; вручную не править.
"""

# Имена вроде TOKEN_INVALID линтер принимает за пароли: это просто коды ошибок.
# ruff: noqa: S105

from enum import StrEnum


class ErrorCode(StrEnum):
    """Код верхнего уровня (`code` в problem+json)."""

    # Общие
    INVALID_REQUEST = "invalid_request"
    INVALID_CURSOR = "invalid_cursor"
    TOKEN_MISSING = "token_missing"
    TOKEN_INVALID = "token_invalid"
    TOKEN_EXPIRED = "token_expired"
    SESSION_REVOKED = "session_revoked"
    ACCOUNT_DELETION_PENDING = "account_deletion_pending"
    CONSENT_REQUIRED = "consent_required"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    REQUEST_IN_PROGRESS = "request_in_progress"
    IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"

    # Аутентификация и аккаунт
    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    ACCOUNT_SUSPENDED = "account_suspended"
    ACCOUNT_BANNED = "account_banned"
    REFRESH_MISSING = "refresh_missing"
    REFRESH_INVALID = "refresh_invalid"
    REFRESH_EXPIRED = "refresh_expired"
    REFRESH_REUSED = "refresh_reused"
    CSRF_FAILED = "csrf_failed"
    REAUTH_FAILED = "reauth_failed"
    TOKEN_INVALID_OR_EXPIRED = "token_invalid_or_expired"
    USERNAME_TAKEN = "username_taken"
    USERNAME_CHANGE_COOLDOWN = "username_change_cooldown"
    NOT_PENDING_DELETION = "not_pending_deletion"
    OAUTH_ALREADY_LINKED = "oauth_already_linked"
    LAST_LOGIN_METHOD = "last_login_method"
    ROLE_MUST_BE_REVOKED = "role_must_be_revoked"
    ONBOARDING_NOT_REQUIRED = "onboarding_not_required"

    # Социальный граф и профили
    SELF_ACTION = "self_action"
    ALREADY_FRIENDS = "already_friends"
    FRIEND_REQUEST_EXISTS = "friend_request_exists"
    FRIEND_REQUEST_NOT_PENDING = "friend_request_not_pending"
    FOLLOW_REQUEST_NOT_PENDING = "follow_request_not_pending"
    LIST_HIDDEN = "list_hidden"
    PROFILE_PRIVATE = "profile_private"

    # Контент
    NOT_AUTHOR = "not_author"
    COMMENTS_FORBIDDEN = "comments_forbidden"

    # Медиа
    QUOTA_EXCEEDED = "quota_exceeded"
    UPLOAD_MISSING = "upload_missing"
    UPLOAD_REJECTED = "upload_rejected"
    ASSET_IN_USE = "asset_in_use"

    # Чат
    DM_FORBIDDEN = "dm_forbidden"
    USER_BLOCKED_BY_YOU = "user_blocked_by_you"
    NOT_GROUP_ADMIN = "not_group_admin"
    NOT_GROUP_OWNER = "not_group_owner"
    CANNOT_REMOVE_OWNER = "cannot_remove_owner"
    EDIT_WINDOW_EXPIRED = "edit_window_expired"
    SYSTEM_MESSAGE = "system_message"
    CONVERSATION_NOT_GROUP = "conversation_not_group"
    CONVERSATION_IS_GROUP = "conversation_is_group"
    GROUP_FULL = "group_full"
    MESSAGE_DELETED = "message_deleted"

    # Модерация и администрирование
    INSUFFICIENT_ROLE = "insufficient_role"
    CANNOT_CHANGE_OWN_ROLE = "cannot_change_own_role"
    ALREADY_CLAIMED = "already_claimed"
    REPORT_NOT_OPEN = "report_not_open"
    ALREADY_HIDDEN = "already_hidden"
    NOT_HIDDEN = "not_hidden"
    INVALID_TRANSITION = "invalid_transition"

    # Права субъекта ⚖️
    EXPORT_IN_PROGRESS = "export_in_progress"
    EXPORT_NOT_READY = "export_not_ready"
    EXPORT_EXPIRED = "export_expired"

    # Реальное время
    TICKET_INVALID = "ticket_invalid"
    FORBIDDEN_ORIGIN = "forbidden_origin"


class ItemCode(StrEnum):
    """Код элемента `errors[].code` у `validation_error`."""

    # Общие
    UNKNOWN_FIELD = "unknown_field"
    REQUIRED = "required"
    STRING_TOO_SHORT = "string_too_short"
    STRING_TOO_LONG = "string_too_long"
    INVALID_FORMAT = "invalid_format"
    OUT_OF_RANGE = "out_of_range"
    INVALID_ENUM = "invalid_enum"
    TOO_MANY_ITEMS = "too_many_items"
    DUPLICATE_ITEMS = "duplicate_items"

    # Регистрация и профиль
    USERNAME_RESERVED = "username_reserved"
    PASSWORD_TOO_WEAK = "password_too_weak"
    UNDERAGE = "underage"
    AGE_NOT_CONFIRMED = "age_not_confirmed"

    # ⚖️ Согласия
    CONSENT_MISSING = "consent_missing"
    CONSENT_VERSION_OUTDATED = "consent_version_outdated"
    INVALID_CATEGORY = "invalid_category"
    BASIC_CATEGORY_REQUIRED = "basic_category_required"

    # Ссылки на медиа
    ASSET_NOT_FOUND = "asset_not_found"
    ASSET_NOT_READY = "asset_not_ready"
    ASSET_WRONG_PURPOSE = "asset_wrong_purpose"
    ASSET_ALREADY_ATTACHED = "asset_already_attached"
    DUPLICATE_MEDIA = "duplicate_media"
    TOO_MANY_MEDIA = "too_many_media"
    TOO_MANY_ATTACHMENTS = "too_many_attachments"

    # Загрузка
    PURPOSE_INVALID = "purpose_invalid"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    EXTENSION_FORBIDDEN = "extension_forbidden"
    SIZE_INVALID = "size_invalid"
    SIZE_EXCEEDS_LIMIT = "size_exceeds_limit"

    # Посты, комментарии, реакции
    BODY_OR_MEDIA_REQUIRED = "body_or_media_required"
    PARENT_INVALID = "parent_invalid"
    EMOJI_NOT_ALLOWED = "emoji_not_allowed"

    # Чат
    BODY_OR_ATTACHMENTS_REQUIRED = "body_or_attachments_required"
    REPLY_NOT_FOUND = "reply_not_found"
    MEMBER_NOT_FOUND = "member_not_found"
    MEMBER_NOT_FRIEND = "member_not_friend"
    MEMBER_DM_FORBIDDEN = "member_dm_forbidden"
    ALREADY_MEMBER = "already_member"
    TOO_MANY_MEMBERS = "too_many_members"
    MULTIPLE_ANCHORS = "multiple_anchors"
    READ_BEYOND_LAST = "read_beyond_last"

    # Уведомления
    IDS_OR_UP_TO_REQUIRED = "ids_or_up_to_required"
    UNKNOWN_TYPE = "unknown_type"

    # Модерация
    UNTIL_REQUIRED = "until_required"
    UNTIL_TOO_FAR = "until_too_far"
    OUTCOME_NOT_APPLICABLE = "outcome_not_applicable"


# Адреса перенаправления OAuth (`/login?error=<код>`): HTTP-статуса у них нет.
OAUTH_REDIRECT_ERRORS: frozenset[str] = frozenset(
    {
        "oauth_failed",
        "oauth_email_conflict",
        "oauth_email_required",
        "terms_required",
    }
)


# (HTTP-статус, краткое стабильное название) для каждого кода.
PROBLEM_SPECS: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.INVALID_REQUEST: (400, "Invalid request"),
    ErrorCode.INVALID_CURSOR: (400, "Invalid cursor"),
    ErrorCode.TOKEN_MISSING: (401, "Token missing"),
    ErrorCode.TOKEN_INVALID: (401, "Token invalid"),
    ErrorCode.TOKEN_EXPIRED: (401, "Token expired"),
    ErrorCode.SESSION_REVOKED: (401, "Session revoked"),
    ErrorCode.ACCOUNT_DELETION_PENDING: (403, "Account deletion pending"),
    ErrorCode.CONSENT_REQUIRED: (403, "Consent required"),
    ErrorCode.NOT_FOUND: (404, "Not found"),
    ErrorCode.METHOD_NOT_ALLOWED: (405, "Method not allowed"),
    ErrorCode.PAYLOAD_TOO_LARGE: (413, "Payload too large"),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: (415, "Unsupported media type"),
    ErrorCode.REQUEST_IN_PROGRESS: (409, "Request in progress"),
    ErrorCode.IDEMPOTENCY_KEY_REUSE: (422, "Idempotency key reuse"),
    ErrorCode.VALIDATION_ERROR: (422, "Validation error"),
    ErrorCode.RATE_LIMITED: (429, "Rate limited"),
    ErrorCode.INTERNAL_ERROR: (500, "Internal error"),
    ErrorCode.SERVICE_UNAVAILABLE: (503, "Service unavailable"),
    ErrorCode.INVALID_CREDENTIALS: (401, "Invalid credentials"),
    ErrorCode.EMAIL_NOT_VERIFIED: (403, "Email not verified"),
    ErrorCode.ACCOUNT_SUSPENDED: (403, "Account suspended"),
    ErrorCode.ACCOUNT_BANNED: (403, "Account banned"),
    ErrorCode.REFRESH_MISSING: (401, "Refresh missing"),
    ErrorCode.REFRESH_INVALID: (401, "Refresh invalid"),
    ErrorCode.REFRESH_EXPIRED: (401, "Refresh expired"),
    ErrorCode.REFRESH_REUSED: (401, "Refresh reused"),
    ErrorCode.CSRF_FAILED: (403, "CSRF check failed"),
    ErrorCode.REAUTH_FAILED: (403, "Reauth failed"),
    ErrorCode.TOKEN_INVALID_OR_EXPIRED: (400, "Token invalid or expired"),
    ErrorCode.USERNAME_TAKEN: (409, "Username taken"),
    ErrorCode.USERNAME_CHANGE_COOLDOWN: (409, "Username change cooldown"),
    ErrorCode.NOT_PENDING_DELETION: (409, "Not pending deletion"),
    ErrorCode.OAUTH_ALREADY_LINKED: (409, "Oauth already linked"),
    ErrorCode.LAST_LOGIN_METHOD: (409, "Last login method"),
    ErrorCode.ROLE_MUST_BE_REVOKED: (409, "Role must be revoked"),
    ErrorCode.ONBOARDING_NOT_REQUIRED: (409, "Onboarding not required"),
    ErrorCode.SELF_ACTION: (400, "Self action"),
    ErrorCode.ALREADY_FRIENDS: (409, "Already friends"),
    ErrorCode.FRIEND_REQUEST_EXISTS: (409, "Friend request exists"),
    ErrorCode.FRIEND_REQUEST_NOT_PENDING: (409, "Friend request not pending"),
    ErrorCode.FOLLOW_REQUEST_NOT_PENDING: (409, "Follow request not pending"),
    ErrorCode.LIST_HIDDEN: (403, "List hidden"),
    ErrorCode.PROFILE_PRIVATE: (403, "Profile private"),
    ErrorCode.NOT_AUTHOR: (403, "Not author"),
    ErrorCode.COMMENTS_FORBIDDEN: (403, "Comments forbidden"),
    ErrorCode.QUOTA_EXCEEDED: (403, "Quota exceeded"),
    ErrorCode.UPLOAD_MISSING: (409, "Upload missing"),
    ErrorCode.UPLOAD_REJECTED: (422, "Upload rejected"),
    ErrorCode.ASSET_IN_USE: (409, "Asset in use"),
    ErrorCode.DM_FORBIDDEN: (403, "DM forbidden"),
    ErrorCode.USER_BLOCKED_BY_YOU: (403, "User blocked by you"),
    ErrorCode.NOT_GROUP_ADMIN: (403, "Not group admin"),
    ErrorCode.NOT_GROUP_OWNER: (403, "Not group owner"),
    ErrorCode.CANNOT_REMOVE_OWNER: (403, "Cannot remove owner"),
    ErrorCode.EDIT_WINDOW_EXPIRED: (403, "Edit window expired"),
    ErrorCode.SYSTEM_MESSAGE: (403, "System message"),
    ErrorCode.CONVERSATION_NOT_GROUP: (409, "Conversation not group"),
    ErrorCode.CONVERSATION_IS_GROUP: (409, "Conversation is group"),
    ErrorCode.GROUP_FULL: (409, "Group full"),
    ErrorCode.MESSAGE_DELETED: (409, "Message deleted"),
    ErrorCode.INSUFFICIENT_ROLE: (403, "Insufficient role"),
    ErrorCode.CANNOT_CHANGE_OWN_ROLE: (403, "Cannot change own role"),
    ErrorCode.ALREADY_CLAIMED: (409, "Already claimed"),
    ErrorCode.REPORT_NOT_OPEN: (409, "Report not open"),
    ErrorCode.ALREADY_HIDDEN: (409, "Already hidden"),
    ErrorCode.NOT_HIDDEN: (409, "Not hidden"),
    ErrorCode.INVALID_TRANSITION: (409, "Invalid transition"),
    ErrorCode.EXPORT_IN_PROGRESS: (409, "Export in progress"),
    ErrorCode.EXPORT_NOT_READY: (409, "Export not ready"),
    ErrorCode.EXPORT_EXPIRED: (410, "Export expired"),
    ErrorCode.TICKET_INVALID: (401, "Ticket invalid"),
    ErrorCode.FORBIDDEN_ORIGIN: (403, "Forbidden origin"),
}
