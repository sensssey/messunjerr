"""Почта и очередь задач: шаблоны, SMTP-адрес, ошибки отправки, сериализация и задача `send_email`."""

import json
from datetime import UTC, datetime
from typing import Any

import aiosmtplib
import pytest
import structlog
from arq.worker import Retry
from jinja2 import TemplateNotFound, UndefinedError
from pydantic import SecretStr

from messunjerr.core.jobs import (
    QUEUE_DEFAULT,
    QUEUE_EMAIL,
    QUEUE_MEDIA,
    QUEUES,
    TASK_CLEANUP_PENDING_UPLOADS,
    TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
    TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
    TASK_DELETE_MEDIA_OBJECTS,
    TASK_PROCESS_MEDIA,
    TASK_RECONCILE_UPLOADS,
    TASK_SEND_EMAIL,
    TASK_SWEEP_ORPHAN_OBJECTS,
    InMemoryJobQueue,
)
from messunjerr.core.mail import (
    InMemoryMailer,
    PermanentMailError,
    SmtpMailer,
    TransientMailError,
    parse_smtp_url,
    render_email,
)
from messunjerr.jobs.queue import json_deserializer, json_serializer, queue_key
from messunjerr.jobs.tasks import (
    SEND_EMAIL_MAX_TRIES,
    SEND_EMAIL_RETRY_BASE_SECONDS,
    send_email,
)
from messunjerr.jobs.worker import build_worker, cron_jobs_for, functions_for
from messunjerr.settings import Settings

SENDER = "messunjerr <no-reply@messunjerr.local>"
VERIFY_URL = "http://localhost:3000/verify-email#token=abc123"
VERIFY_CONTEXT = {
    "verify_url": VERIFY_URL,
    "token": "abc123",
    "ttl_hours": 24,
}


def settings() -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        database_url=SecretStr("postgresql+asyncpg://app:p@h/db"),
        redis_url=SecretStr("redis://h/0"),
        mail_from=SENDER,
    )


# ----------------------------------------------------------------------------- шаблоны
def bodies(message: Any) -> tuple[str, str]:
    text = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    return text, html


def test_verification_email_is_russian_and_carries_link_and_code() -> None:
    message = render_email("verify_email", VERIFY_CONTEXT, sender=SENDER, to="user@example.com")
    text, html = bodies(message)
    assert message["Subject"] == "Подтвердите адрес электронной почты в messunjerr"
    assert message["To"] == "user@example.com"
    assert message["From"] == SENDER
    assert VERIFY_URL in text
    assert VERIFY_URL in html
    assert "abc123" in text
    assert "24 ч" in text
    assert "Здравствуйте" in text
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["Message-ID"].endswith("@messunjerr.local>")


def test_html_part_escapes_values_but_text_part_does_not() -> None:
    hostile = {**VERIFY_CONTEXT, "verify_url": "http://x/?a=1&b=<script>alert(1)</script>"}
    text, html = bodies(render_email("verify_email", hostile, sender=SENDER, to="u@example.com"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;b=" in html
    assert "a=1&b=<script>" in text


def test_account_exists_email() -> None:
    context = {
        "login_url": "http://localhost:3000/login",
        "reset_url": "http://localhost:3000/forgot-password",
    }
    message = render_email("account_exists", context, sender=SENDER, to="user@example.com")
    text, html = bodies(message)
    assert message["Subject"] == "Вы уже зарегистрированы в messunjerr"
    assert context["login_url"] in text
    assert context["reset_url"] in html


SECURITY_MAILS: dict[str, tuple[dict[str, Any], str, list[str]]] = {
    "reset_password": (
        {"reset_url": "http://x/reset-password#token=tok", "token": "tok", "ttl_minutes": 60},
        "Сброс пароля в messunjerr",
        ["http://x/reset-password#token=tok", "tok", "60 минут", "все ваши сессии будут закрыты"],
    ),
    "password_changed": (
        {
            "when": "05.10.2026 12:34 UTC",
            "login_url": "http://x/login",
            "reset_url": "http://x/forgot",
        },
        "Пароль изменён в messunjerr",
        ["05.10.2026 12:34 UTC", "http://x/forgot", "http://x/login"],
    ),
    "refresh_reuse": (
        {
            "device": "Firefox на Windows",
            "login_url": "http://x/login",
            "reset_url": "http://x/forgot",
        },
        "Подозрительная активность: сессия в messunjerr закрыта",
        ["Firefox на Windows", "http://x/login", "http://x/forgot", "использован повторно"],
    ),
    "email_change_confirm": (
        {"confirm_url": "http://x/confirm-email#token=tok", "token": "tok", "ttl_minutes": 60},
        "Подтвердите новый адрес почты в messunjerr",
        ["http://x/confirm-email#token=tok", "tok", "60 минут"],
    ),
    "email_change_notice": (
        {"new_email_masked": "n***@example.org", "reset_url": "http://x/forgot"},
        "Запрошена смена почты в messunjerr",
        ["n***@example.org", "http://x/forgot"],
    ),
    "email_changed": (
        {"new_email_masked": "n***@example.org", "reset_url": "http://x/forgot"},
        "Почта аккаунта в messunjerr изменена",
        ["n***@example.org", "http://x/forgot"],
    ),
    "account_deletion_requested": (
        {
            "scheduled_at": "19.10.2026 12:34 UTC",
            "grace_days": 14,
            "login_url": "http://x/login",
            "reset_url": "http://x/forgot",
        },
        "Удаление аккаунта в messunjerr",
        ["19.10.2026 12:34 UTC", "14 дн.", "http://x/login", "http://x/forgot", "восстановить"],
    ),
}


@pytest.mark.parametrize("name", sorted(SECURITY_MAILS))
def test_security_emails_are_russian_and_carry_their_links(name: str) -> None:
    context, subject, fragments = SECURITY_MAILS[name]
    message = render_email(name, context, sender=SENDER, to="user@example.com")
    text, html = bodies(message)
    assert message["Subject"] == subject
    assert "Здравствуйте" in text
    for fragment in fragments:
        assert fragment in text, fragment
    assert "<html" in html
    assert fragments[0] in html


def test_reuse_notice_works_without_a_device_label() -> None:
    context = {"device": None, "login_url": "http://x/login", "reset_url": "http://x/forgot"}
    text, _ = bodies(render_email("refresh_reuse", context, sender=SENDER, to="user@example.com"))
    assert "None" not in text
    assert "()" not in text


def test_a_missing_template_variable_is_an_error_not_an_empty_gap() -> None:
    with pytest.raises(UndefinedError):
        render_email("verify_email", {}, sender=SENDER, to="user@example.com")


def test_unknown_template() -> None:
    with pytest.raises(TemplateNotFound):
        render_email("no_such_template", {}, sender=SENDER, to="user@example.com")


def test_header_injection_through_the_recipient_is_rejected() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 (текст ошибки стандартной библиотеки)
        render_email(
            "verify_email", VERIFY_CONTEXT, sender=SENDER, to="a@example.com\nBcc: evil@example.org"
        )


# ----------------------------------------------------------------------------- SMTP
def test_plain_smtp_url_for_mailpit() -> None:
    config = parse_smtp_url("smtp://mailpit:1025")
    assert (config.hostname, config.port) == ("mailpit", 1025)
    assert config.username is None
    assert config.use_tls is False
    assert config.start_tls is None


def test_smtps_url_with_credentials_is_decoded() -> None:
    config = parse_smtp_url("smtps://user%40example.ru:p%2Fss@smtp.example.ru")
    assert config.port == 465
    assert config.use_tls is True
    assert (config.username, config.password) == ("user@example.ru", "p/ss")
    assert "p/ss" not in repr(config)


def test_starttls_url() -> None:
    config = parse_smtp_url("smtp+starttls://smtp.example.ru")
    assert config.port == 587
    assert config.start_tls is True
    assert config.use_tls is False


@pytest.mark.parametrize("url", ["http://mail", "smtp://", "mailpit:1025", ""])
def test_unsupported_smtp_urls(url: str) -> None:
    with pytest.raises(ValueError, match="SMTP_URL"):
        parse_smtp_url(url)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (aiosmtplib.SMTPRecipientsRefused([]), PermanentMailError),
        (aiosmtplib.SMTPResponseException(550, "mailbox unavailable"), PermanentMailError),
        (aiosmtplib.SMTPAuthenticationError(535, "bad credentials"), PermanentMailError),
        (aiosmtplib.SMTPResponseException(451, "try later"), TransientMailError),
        (aiosmtplib.SMTPServerDisconnected("gone"), TransientMailError),
        (aiosmtplib.SMTPTimeoutError("slow"), TransientMailError),
        (ConnectionRefusedError("down"), TransientMailError),
    ],
)
async def test_smtp_errors_are_split_into_permanent_and_transient(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[Exception]
) -> None:
    async def failing_send(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(aiosmtplib, "send", failing_send)
    message = render_email("verify_email", VERIFY_CONTEXT, sender=SENDER, to="u@example.com")
    with pytest.raises(expected):
        await SmtpMailer(parse_smtp_url("smtp://mailpit:1025")).send(message)


async def test_smtp_mailer_passes_the_configuration_to_the_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_send(message: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    message = render_email("verify_email", VERIFY_CONTEXT, sender=SENDER, to="u@example.com")
    await SmtpMailer(parse_smtp_url("smtps://u:p@smtp.example.ru:2465"), timeout=3).send(message)
    assert captured == {
        "hostname": "smtp.example.ru",
        "port": 2465,
        "username": "u",
        "password": "p",
        "use_tls": True,
        "start_tls": None,
        "timeout": 3,
    }


# ----------------------------------------------------------------------------- очередь
async def test_in_memory_queue_records_jobs_and_drops_duplicate_ids() -> None:
    queue = InMemoryJobQueue()
    assert await queue.enqueue("task", queue=QUEUE_EMAIL, job_id="same", value=1) is True
    assert await queue.enqueue("task", queue=QUEUE_EMAIL, job_id="same", value=2) is False
    assert await queue.enqueue("task", value=3) is True
    assert [job.kwargs["value"] for job in queue.named("task")] == [1, 3]
    assert queue.jobs[0].queue == QUEUE_EMAIL
    assert queue.jobs[1].queue == QUEUE_DEFAULT
    queue.clear()
    assert queue.jobs == []


async def test_in_memory_queue_can_simulate_an_outage() -> None:
    queue = InMemoryJobQueue(fail_with=ConnectionError("redis is down"))
    with pytest.raises(ConnectionError):
        await queue.enqueue("task")


def test_queues_use_the_documented_names_and_keys() -> None:
    assert QUEUES == ("email", "default", "media")
    assert queue_key("email") == "arq:queue:email"
    assert TASK_SEND_EMAIL == "send_email"


def test_job_payload_is_json_with_utf8_and_never_pickle() -> None:
    payload = {"f": "send_email", "k": {"to": "тест@пример.рф", "context": {"n": 1}}, "t": 1}
    raw = json_serializer(payload)
    assert isinstance(raw, bytes)
    assert json.loads(raw) == payload
    assert "тест@пример.рф" in raw.decode("utf-8")
    assert json_deserializer(raw) == payload


def test_non_json_values_fail_at_enqueue_time() -> None:
    with pytest.raises(TypeError):
        json_serializer({"k": {"when": datetime(2026, 10, 5, tzinfo=UTC)}})


def test_email_queue_has_the_send_email_task_with_retries_and_no_stored_result() -> None:
    (function,) = functions_for(QUEUE_EMAIL)
    assert function.name == TASK_SEND_EMAIL
    assert function.max_tries == SEND_EMAIL_MAX_TRIES == 5
    assert function.keep_result_s == 0  # в аргументах токен: результат в Redis не оставляем


def test_a_queue_without_tasks_does_not_get_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Все очереди проекта теперь с задачами; пустую изображаем подменой реестра.
    def nothing(_queue: str) -> list[Any]:
        return []

    monkeypatch.setattr("messunjerr.jobs.worker.functions_for", nothing)
    monkeypatch.setattr("messunjerr.jobs.worker.cron_jobs_for", nothing)
    with pytest.raises(RuntimeError, match="нет задач"):
        build_worker(QUEUE_MEDIA, settings())


def test_the_media_queue_has_its_tasks_and_no_schedule() -> None:
    assert {function.name for function in functions_for(QUEUE_MEDIA)} == {
        TASK_PROCESS_MEDIA,
        TASK_DELETE_MEDIA_OBJECTS,
    }
    assert cron_jobs_for(QUEUE_MEDIA) == []
    for function in functions_for(QUEUE_MEDIA):
        assert function.keep_result_s == 0  # повторная постановка после итога должна проходить
    process = next(f for f in functions_for(QUEUE_MEDIA) if f.name == TASK_PROCESS_MEDIA)
    assert (process.max_tries, process.timeout_s) == (3, 120)  # 4.12
    delete = next(f for f in functions_for(QUEUE_MEDIA) if f.name == TASK_DELETE_MEDIA_OBJECTS)
    # Самый долгий тайм-аут воркера задаёт время, пока после его смерти задачу никто не возьмёт.
    assert (delete.max_tries, delete.timeout_s) == (5, 120)


def test_default_queue_runs_housekeeping_on_a_schedule() -> None:
    jobs = {job.name: job for job in cron_jobs_for(QUEUE_DEFAULT)}

    assert set(jobs) == {
        TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
        TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
        TASK_CLEANUP_PENDING_UPLOADS,
        TASK_RECONCILE_UPLOADS,
        TASK_SWEEP_ORPHAN_OBJECTS,
    }
    sweep = jobs[TASK_SWEEP_ORPHAN_OBJECTS]
    assert (sweep.hour, sweep.minute) == (4, 20)  # раз в сутки, ночью
    nightly = jobs[TASK_CLEANUP_UNVERIFIED_ACCOUNTS]
    hourly = jobs[TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY]
    assert (nightly.hour, nightly.minute) == (3, 10)  # раз в сутки, ночью
    assert (hourly.hour, hourly.minute) == (None, 17)  # каждый час
    uploads = jobs[TASK_CLEANUP_PENDING_UPLOADS]
    assert (uploads.hour, uploads.minute) == (None, 41)  # каждый час (4.12)
    reconcile = jobs[TASK_RECONCILE_UPLOADS]
    assert reconcile.hour is None
    assert reconcile.minute == set(range(0, 60, 5))  # каждые 5 минут
    for job in jobs.values():
        assert job.unique  # несколько воркеров не запускают одну задачу дважды
        assert job.keep_result_s == 0
        assert job.max_tries == 1  # следующий запуск и так придёт по расписанию
    assert cron_jobs_for(QUEUE_EMAIL) == []


def test_the_default_worker_is_built_from_cron_jobs_and_keeps_schedules_in_utc() -> None:
    worker = build_worker(QUEUE_DEFAULT, settings(), handle_signals=False)

    assert set(worker.functions) == {
        TASK_CLEANUP_UNVERIFIED_ACCOUNTS,
        TASK_CLEANUP_TOKENS_AND_IDEMPOTENCY,
        TASK_CLEANUP_PENDING_UPLOADS,
        TASK_RECONCILE_UPLOADS,
        TASK_SWEEP_ORPHAN_OBJECTS,
    }
    assert worker.timezone is UTC
    assert worker.queue_name == "arq:queue:default"


# ----------------------------------------------------------------------------- задача send_email
def ctx(mailer: InMemoryMailer, attempt: int = 1) -> dict[str, Any]:
    return {"settings": settings(), "mailer": mailer, "job_try": attempt}


async def test_send_email_delivers_the_rendered_message() -> None:
    mailer = InMemoryMailer()
    await send_email(
        ctx(mailer), to="user@example.com", template="verify_email", context=VERIFY_CONTEXT
    )
    (message,) = mailer.sent
    assert message["To"] == "user@example.com"
    assert message["From"] == SENDER
    assert "Подтвердите" in message["Subject"]


@pytest.mark.parametrize(("attempt", "delay"), [(1, 30), (2, 60), (3, 120), (4, 240)])
async def test_transient_failure_retries_with_exponential_backoff(attempt: int, delay: int) -> None:
    mailer = InMemoryMailer(fail_with=TransientMailError("SMTPServerDisconnected"))
    with pytest.raises(Retry) as caught:
        await send_email(
            ctx(mailer, attempt),
            to="user@example.com",
            template="verify_email",
            context=VERIFY_CONTEXT,
        )
    assert caught.value.defer_score == delay * 1000
    assert SEND_EMAIL_RETRY_BASE_SECONDS == 30


async def test_after_the_last_attempt_the_task_fails_instead_of_retrying() -> None:
    mailer = InMemoryMailer(fail_with=TransientMailError("SMTPServerDisconnected"))
    with pytest.raises(TransientMailError):
        await send_email(
            ctx(mailer, SEND_EMAIL_MAX_TRIES),
            to="user@example.com",
            template="verify_email",
            context=VERIFY_CONTEXT,
        )


async def test_rejected_recipient_is_not_retried() -> None:
    mailer = InMemoryMailer(fail_with=PermanentMailError("SMTPRecipientsRefused"))
    await send_email(
        ctx(mailer), to="user@example.com", template="verify_email", context=VERIFY_CONTEXT
    )
    assert mailer.sent == []


async def test_broken_template_is_not_retried() -> None:
    mailer = InMemoryMailer()
    await send_email(ctx(mailer), to="user@example.com", template="no_such_template", context={})
    await send_email(ctx(mailer), to="user@example.com", template="verify_email", context={})
    assert mailer.sent == []


async def test_logs_never_contain_the_recipient_or_the_token() -> None:
    mailer = InMemoryMailer(fail_with=PermanentMailError("SMTPRecipientsRefused"))
    with structlog.testing.capture_logs() as logs:
        await send_email(
            ctx(mailer),
            to="secret.person@example.com",
            template="verify_email",
            context=VERIFY_CONTEXT,
        )
        await send_email(
            ctx(InMemoryMailer()),
            to="secret.person@example.com",
            template="verify_email",
            context=VERIFY_CONTEXT,
        )
    dump = json.dumps(logs, default=str)
    assert logs
    assert "secret.person" not in dump
    assert "abc123" not in dump
