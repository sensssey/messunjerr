"""Действия, которые контекст identity пишет в журнал аудита (4.14)."""

# Имена вроде PASSWORD_CHANGED линтер принимает за пароли: это просто названия действий.
# ruff: noqa: S105

LOGIN_SUCCESS = "login.success"
LOGIN_FAILURE = "login.failure"
LOGOUT = "logout"
LOGOUT_ALL = "logout_all"
SESSION_REVOKED = "session.revoked"
REFRESH_REUSE_DETECTED = "refresh.reuse_detected"
REAUTH_FAILURE = "reauth.failure"
PASSWORD_RESET_REQUESTED = "password.reset_requested"
PASSWORD_RESET = "password.reset"
PASSWORD_CHANGED = "password.changed"
EMAIL_CHANGE_REQUESTED = "email.change_requested"
EMAIL_CHANGED = "email.changed"
ACCOUNT_UNVERIFIED_PURGED = "account.unverified_purged"

TARGET_USER = "user"
TARGET_SESSION = "session"
