# Журнал изменений API v2

> Контракт API до релиза 1.0 можно менять, но каждое изменение записывается здесь (план спринтов, 2.3): фронтенд не должен узнавать о нём из ошибок. Новые записи сверху. Ссылки вида «5.3» ведут в разделы [backend-v2-spec.md](backend-v2-spec.md), «S3-02» в задачи [плана спринтов](backend-v2-sprints.md). Журнал ведётся с S3; что было до него, записано в итогах спринтов S0–S2 плана.

Знаки: ➕ добавлено · ✏️ изменено · ⚠️ отличается от спецификации (причина рядом) · 🧭 что делать клиенту.

---

## S3. Профили и приватность · 2026-10-05

### ➕ Новые ручки (семь)

| Ручка | Что делает | Ошибки |
|---|---|---|
| `PATCH /me/profile` | правка профиля как JSON Merge Patch: `display_name`, `bio`, `links`, `birth_date`, `birth_date_visibility`, `city`, `language`, `timezone`, `is_private`, `avatar_asset_id`; ответ `200` с профилем | `422`: `underage`, `out_of_range`, `asset_not_found`, `invalid_format`, `string_too_long`, `too_many_items`, `unknown_field` и др. |
| `GET /me/privacy` | настройки приватности владельца | |
| `PATCH /me/privacy` | любые из семи настроек, значения из перечислений; ответ `200` с настройками целиком | `422` `invalid_enum`, `invalid_format` (для `null`) |
| `PATCH /me/username` | смена ника; ответ `200` `{ "username": "…" }` | `409 username_taken`, `409 username_change_cooldown` (поле `retry_after_days`), `422 username_reserved` |
| `DELETE /me` | запрос удаления аккаунта; тело `{ "password": "…" }`; ответ `202` `{ "deletion_scheduled_at": "…" }` | `403 reauth_failed`, `409 role_must_be_revoked`, `422` |
| `POST /me/restore` | отмена удаления до срока; ответ `200` с полным `MeUser` | `409 not_pending_deletion` |
| `GET /users/{ref}` | профиль человека глазами зрителя; `{ref}` это UUID или ник | `404 not_found` |

### ✏️ Изменённые ответы и поведение

- **`user` в ответах `POST /auth/login`, `/auth/refresh`, `/auth/verify-email` и тело `GET /me`** теперь полный `MeUser` (5.3): к полям `id`, `username`, `email`, `email_verified`, `role`, `status`, `created_at` добавились `profile`, `privacy`, `counters` и `required_actions`. Прежние поля остались на месте и не изменились, поэтому клиент S2 продолжает работать. `counters` пока все нули (друзья, уведомления и беседы появятся в S7, S10, S14), `required_actions` всегда `[]` (реестр согласий в бэклоге, B-01), `profile.hidden_fields` всегда `[]`.
- **`POST /auth/register`** принимает необязательные `display_name` (1–50 символов, по умолчанию равно нику), `language` (BCP 47, хранится в каноническом написании: `en-us` становится `en-US`) и `timezone` (имя из базы IANA, регистр важен). Профиль и настройки приватности создаются в той же транзакции, что и аккаунт. Поля `consents` и `age_confirmed` из 5.2 по-прежнему не принимаются (`unknown_field`): в упрощённой юридической части есть только `accept_terms` (план 1.2).
- **`GET /auth/username-available`** и **регистрация**: прежний ник после смены остаётся занятым 30 дней, ответ `taken`, регистрация и смена ника на него дают `409 username_taken`.
- **Аккаунт в статусе `deletion_pending`** (после `DELETE /me`) может вызывать только `GET /me`, `POST /me/restore` и `POST /auth/logout` (а ещё входить и обновлять токен); любая другая ручка с токеном отвечает `403 account_deletion_pending` (5.1). Вход таким аккаунтом разрешён, `user.status` в ответе равен `deletion_pending`: клиент предлагает восстановление. Профиль такого аккаунта для других людей `404`.
- **`GET /api/v1/meta`**: в `limits` добавлены `display_name_max` (50), `city_max` (100), `link_title_max` (40), `link_url_max` (300). Клиенту не нужно хардкодить границы полей профиля.
- Лимиты: все новые ручки считаются в бакетах `api_read` (чтение) и `api_write` (изменение); `DELETE /me` ещё и «чувствительная» операция, без Redis она отвечает `503`.

### ⚠️ Отличия от спецификации (и причины)

- `PATCH /me/profile`: `avatar_asset_id` с любым значением, кроме `null`, даёт `422 asset_not_found`: ресурсов (`media.assets`) нет до S5–S6 (так записано в задаче S3-02).
- `GET /users/{ref}`: `relationship` всегда «чужой» (`friendship: none`, `following: none`, `follows_you: false`, `blocked: false`), `presence` всегда `null`, счётчики равны нулю (S7–S8, S11, S16).
- Счётчик `following` в `UserProfile.counters` подчиняется настройке `followers_list_visibility`: отдельной настройки «кто видит мои подписки» в модели нет.
- Закрытость профиля не скрывает счётчики друзей, подписчиков и подписок: их скрывают только настройки владельца (`friends_list_visibility`, `followers_list_visibility`). Сами списки у закрытого профиля чужим недоступны. Счётчик постов у закрытого профиля скрыт от посторонних (`null`), как в 5.3.
- Пустая строка в `bio` и `city` очищает поле (в ответе `null`); `links: null` равно пустому списку.
- `DELETE /me`: тело необязательно, но обязательно `password`, если у аккаунта есть пароль (`422 required` по `/body/password`). Аккаунту без пароля (вход только через OAuth, S21) достаточно сессии не старше пяти минут.
### 🧭 Что делать клиенту

- После входа и `refresh` брать профиль, приватность и счётчики из `user`, отдельный `GET /me` нужен только для обновления.
- На `403 account_deletion_pending` показывать «аккаунт ждёт удаления» с кнопкой «Восстановить» (`POST /me/restore`), а не общую ошибку.
- Для проверки ника при смене пользоваться `GET /auth/username-available` (учитывает резервы); при `409 username_change_cooldown` показывать `retry_after_days`.
- Границы полей профиля брать из `GET /meta` (`limits`).
