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

---

## S5. Медиа I: загрузка · 2026-10-07

### ➕ Новые ручки (пять)

| Ручка | Что делает | Ошибки |
|---|---|---|
| `POST /media/uploads` | заявка на загрузку: `{ purpose, filename, content_type, size_bytes }`; ответ `201` `{ asset, upload }` с заголовком `Location`; принимает `Idempotency-Key`; лимит `upload_init` (60 в час) | `403 quota_exceeded` (`limit`, `used`), `422` (`purpose_invalid`, `content_type_not_allowed`, `extension_forbidden`, `size_invalid`, `size_exceeds_limit` с `meta.max_bytes`; все проблемы сразу), `503` |
| `POST /media/uploads/{asset_id}/complete` | завершить загрузку: сервер проверяет объект, ответ `202` `{ asset }` (`uploaded`); повтор возвращает текущее состояние | `404`, `409 upload_missing`, `422 upload_rejected` (поле `reason`), `503` |
| `GET /media/{asset_id}` | карточка своего ресурса (`Asset`), для опроса статуса | `404` |
| `DELETE /media/{asset_id}` | удалить свой ресурс, `204`; объекты из хранилища убирает фоновая задача | `404`, `409 asset_in_use` |
| `GET /media/quota` | `{ used_bytes, limit_bytes, assets_count }` | |

### ✏️ Что важно знать

- **Загрузка идёт мимо API.** Клиент делает `PUT` файла на `upload.url` с заголовками из `upload.headers` (срок `upload.expires_at`, 15 минут). Подпись закрепляет `Content-Type`, **точный** размер и запись один раз (`If-None-Match: *`, этот заголовок тоже в `upload.headers`): другой тип или размер, а также запрос без заголовка дают `403` от хранилища, а повторный `PUT` по той же ссылке `412`, потому что файл уже на месте (проверенный файл подменить нельзя). У не-изображений в `upload.headers` всегда `Content-Type: application/octet-stream`, заявленный тип остаётся в карточке.
- **Статусы:** `pending` → `uploaded` → `processing` → `ready` | `rejected`; `deleted` для удалённых. До `ready` все `urls` равны `null`; в S5 они пусты и у готовых (ссылки выдаст S6). В S5 «готов» значит «прошёл проверку по сигнатуре»: SVG, HTML и прочее под видом изображения получают `rejected` с `not_an_image`, исполняемые файлы `forbidden_type`, GIF как аватар `unsupported_format`.
- **Квота** считается по готовым файлам и по заявленному размеру идущих загрузок; незавершённая загрузка освобождает место через 24 часа или после `DELETE`.
- **`GET /media/quota`** объявлена раньше `GET /media/{asset_id}`: слово `quota` идентификатором не считается.
- **Лимиты:** `upload_init` (60 заявок в час на человека) вдобавок к `api_write`; `complete` и `DELETE` считаются в `api_write`, чтение в `api_read`.

### ⚠️ Отличия от спецификации (и причины)

- `GET /media/{asset_id}/urls` и публичные адреса аватаров появятся в S6 вместе с обработкой изображений.
- `PATCH /me/profile` по-прежнему отвечает `asset_not_found` на любой `avatar_asset_id`: порт аватара подключит S6 (задача S6-04).
- Событий `media.ready` и `media.rejected` по SSE пока нет (S10): статус читается опросом `GET /media/{asset_id}`.

### 🧭 Что делать клиенту

- Цепочка: заявка → `PUT` по `upload.url` (все заголовки из `upload.headers`, ровно столько байт, сколько заявлено) → `complete` → опрашивать `GET /media/{id}` до `ready` или `rejected`.
- Лимиты размера брать из `GET /meta` (`limits.avatar_max_bytes`, `image_max_bytes`, `file_max_bytes`, `quota_bytes`) и проверять до заявки; запрещённые расширения (`exe`, `bat`, `cmd`, `scr`, `msi`, `ps1`, `js`, `vbs`, `jar`, `apk`) в `/meta` не перечислены, их отвергает `422 extension_forbidden`.
- К `PUT` добавлять только заголовки из `upload.headers`: заголовки о содержимом (`Content-Encoding`, `Content-Disposition`, `Cache-Control`, `Expires`, `Content-Language`) прокси срезает, неподписанные `X-Amz-*` хранилище отклоняет.
- Повтор запроса заявки с тем же `Idempotency-Key` вернёт ту же ссылку, и после 15 минут она уже не действует: нужна новая заявка под другим ключом (прежняя освободит квоту через 24 часа или по `DELETE`).
- Ресурс может долго оставаться в `processing`, если хранилище или воркер были недоступны: файл не отклоняется из-за сбоя инфраструктуры, обработка повторяется сама; опрашивать `GET /media/{id}` с паузой и не пересоздавать загрузку.
- На `403` при `PUT` просить новую ссылку (`POST /media/uploads`), а не повторять старую; на `412` считать файл загруженным и звать `complete` (после обрыва ответа повтор выглядит именно так); на `409 upload_missing` дозагрузить и повторить `complete`.
