# Bridge Manager + Xray Bridge — Yandex Limited Edition (RU)

Форк bridge-manager-xray с фичей **ограничения трафика на уровне пользователя** для аварийных/дорогих bridge-node (Yandex Cloud).

Готовое решение для схемы:

1. Клиент -> Bridge: по умолчанию `VLESS + REALITY` на `:443`; опционально `VLESS + XHTTP + TLS`
2. Bridge -> Exit: `VLESS + XHTTP + TLS` на `s1.bytestand.fun:443`, `path=/bridge-xh`
3. Bridge Manager (FastAPI): выпуск/удаление ключей, хранение в SQLite, управление users в inbound Xray.

Важно:
- На exit-node ничего не меняется.
- Xray работает на хосте через systemd (не docker).
- REST API по умолчанию слушает только `127.0.0.1:8080`.
- Для новых REALITY-нод можно использовать preset через `REALITY_PROFILE`, не меняя старый дефолт.

---

## Дефолтные параметры exit-node (переопределяемые через env)

Значения по умолчанию — ваш текущий exit-сервер. Все параметры можно переопределить при запуске bootstrap:

| Переменная | Дефолт | Описание |
|---|---|---|
| `EXIT_HOST` | `s1.bytestand.fun` | Хост exit-node |
| `EXIT_PORT` | `443` | Порт exit-node |
| `EXIT_PATH` | `/bridge-xh` | XHTTP path до exit |
| `EXIT_SERVER_NAME` | *(равен EXIT_HOST)* | TLS SNI к exit |
| `BRIDGE_UUID_FOR_EXIT` | `7d28c9a1-...` | UUID bridge-клиента на exit |

Транспорт bridge→exit всегда `VLESS + XHTTP + TLS` (не меняется).

---

## Требования перед деплоем на чистый сервер

1. Ubuntu 24.04 (root/sudo доступ).
2. Домен bridge (например `test-bridge.example.com`) уже указывает A-записью на IP этого сервера.
3. Порты открыты снаружи:
- `22/tcp`
- `80/tcp` (только при `USER_MODE=xhttp` — для выпуска сертификата)
- `443/tcp`

Если API хотите открыть наружу, потребуется также `8080/tcp`.

---

## Быстрый запуск (чистый сервер)

### 1. Клонировать репозиторий

```bash
sudo git clone https://github.com/MakzonorX/bridge-manager-xray-yandex-limited.git /opt/bridge-manager
cd /opt/bridge-manager
```

### 2. Запустить bootstrap

Минимальный запуск (режим REALITY, API только локально):

```bash
sudo BRIDGE_DOMAIN=bridge.example.com \
API_TOKEN='очень_длинный_секрет' \
./scripts/bootstrap_bridge.sh
```

Рекомендуемый запуск для новой REALITY-ноды: профиль выбирается через preset, а API можно открыть только для нужных IP/CIDR:

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
API_TOKEN='очень_длинный_секрет' \
API_PUBLIC=true \
API_ALLOW_FROM='198.51.100.10,198.51.100.0/24' \
USER_MODE=reality \
USER_PORT=443 \
REALITY_PROFILE=auto_ru \
EXIT_HOST='s1.bytestand.fun' \
EXIT_PORT=443 \
EXIT_PATH='/bridge-xh' \
BRIDGE_UUID_FOR_EXIT='7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0' \
./scripts/bootstrap_bridge.sh
```

Полная команда для `USER_MODE=xhttp`, API открыт наружу, кастомный exit-node:

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
ACME_EMAIL='admin@example.com' \
API_TOKEN='очень_длинный_секрет' \
API_PUBLIC=true \
USER_MODE=xhttp \
USER_PORT=443 \
USER_PATH='/user-xh' \
EXIT_HOST='s1.bytestand.fun' \
EXIT_PORT=443 \
EXIT_PATH='/bridge-xh' \
EXIT_SERVER_NAME='s1.bytestand.fun' \
BRIDGE_UUID_FOR_EXIT='7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0' \
./scripts/bootstrap_bridge.sh
```

То же, но с режимом REALITY и явным custom override без preset:

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
API_TOKEN='очень_длинный_секрет' \
API_PUBLIC=true \
USER_MODE=reality \
USER_PORT=443 \
REALITY_SERVER_NAME='ads.x5.ru' \
REALITY_DEST='ads.x5.ru:443' \
EXIT_HOST='s1.bytestand.fun' \
EXIT_PORT=443 \
EXIT_PATH='/bridge-xh' \
BRIDGE_UUID_FOR_EXIT='7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0' \
./scripts/bootstrap_bridge.sh
```

### 3. Что делает bootstrap

Скрипт автоматически:

1. Ставит зависимости (`curl`, `ufw`, `python3-venv`, и т.д.).
2. Включает time sync.
3. Настраивает firewall (`22` и user-port; `80` только при `USER_MODE=xhttp`; `8080` только если `API_PUBLIC=true`, при необходимости с allow-list через `API_ALLOW_FROM`).
4. Ставит Xray `v26.2.6` в `/usr/local/bin/xray`.
5. *(Только `USER_MODE=xhttp`)* Выпускает Let's Encrypt сертификат через `acme.sh` (standalone).
6. Для `USER_MODE=reality` вычисляет effective profile (`REALITY_PROFILE`) либо принимает явные `REALITY_SERVER_NAME` / `REALITY_DEST`.
7. Пишет Xray-конфиг bridge в `/usr/local/etc/xray/config.json` (в соответствии с `USER_MODE`).
8. Сохраняет effective env в `/etc/bridge-manager/env`, чтобы повторный bootstrap не регенерировал ключи/short-id без необходимости.
9. Поднимает `xray.service` и `bridge-manager.service`.
10. Поднимает `bridge-manager-tc.service`, чтобы tc shaping переживал reboot.
11. Включает `/healthz` и `GET /v1/system/diagnostics` для быстрой проверки effective profile/firewall/tc.

---

## Переменные bootstrap

### Обязательные

| Переменная | Описание |
|---|---|
| `BRIDGE_DOMAIN` | Домен этого bridge-сервера (A-запись уже указывает на IP) |
| `API_TOKEN` | Секретный токен для `Authorization: Bearer` |
| `ACME_EMAIL` | Email для Let's Encrypt — **обязателен только при `USER_MODE=xhttp`** |

### Протокол клиент → bridge (`USER_MODE`)

| Переменная | Значения | По умолчанию | Описание |
|---|---|---|---|
| `USER_MODE` | `reality` / `xhttp` | `reality` | Транспорт для входящих пользовательских подключений |
| `USER_PORT` | число | `443` | Порт inbound |
| `USER_PATH` | строка | `/user-xh` | XHTTP path (только `USER_MODE=xhttp`) |
| `USER_FLOW` | строка | `xtls-rprx-vision` | Flow (только `USER_MODE=reality`) |
| `USER_HOST_FOR_URI` | строка | *(BRIDGE_DOMAIN)* | Хост в генерируемых vless:// ссылках |

**REALITY-параметры** (только при `USER_MODE=reality`):

| Переменная | По умолчанию | Описание |
|---|---|---|
| `REALITY_PROFILE` | `legacy_x5` | Preset для новых нод: `legacy_x5`, `max_ru`, `mail_ru`, `vk_com`, `auto_ru` |
| `REALITY_SERVER_NAME` | из `REALITY_PROFILE` | Явный SNI/serverName. Имеет приоритет над preset |
| `REALITY_DEST` | `REALITY_SERVER_NAME:443` | Явный dest. Имеет приоритет над preset |
| `REALITY_SHORT_ID` | *(случайный hex)* | Short ID REALITY |
| `REALITY_PRIVATE_KEY` | *(генерируется или переиспользуется)* | x25519 private key |
| `REALITY_PUBLIC_KEY` | *(выводится из private)* | x25519 public key |
| `REALITY_FINGERPRINT` | `chrome` | TLS fingerprint |
| `REALITY_SPIDER_X` | `/` | spiderX |

Рекомендуемая практика:
- Для старого поведения ничего не указывать: останется `legacy_x5`.
- Для новых bridge-нод использовать `REALITY_PROFILE=max_ru` или `REALITY_PROFILE=auto_ru`.
- Если нужен полностью ручной профиль, задавайте сразу `REALITY_SERVER_NAME` и `REALITY_DEST`.

### Параметры exit-node (bridge → exit)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `EXIT_HOST` | `s1.bytestand.fun` | Хост exit-ноды |
| `EXIT_PORT` | `443` | Порт exit-ноды |
| `EXIT_PATH` | `/bridge-xh` | XHTTP path на exit |
| `EXIT_SERVER_NAME` | *(равен EXIT_HOST)* | TLS SNI при подключении к exit |
| `BRIDGE_UUID_FOR_EXIT` | `7d28c9a1-...` | UUID этого bridge на exit-ноде |

### API и система

| Переменная | По умолчанию | Описание |
|---|---|---|
| `API_PUBLIC` | `false` | `true` → API на `0.0.0.0:8080`, UFW открывает порт |
| `API_ALLOW_FROM` | пусто | Список IP/CIDR через запятую для UFW на `8080/tcp`, если `API_PUBLIC=true` |
| `DISABLE_IPV6` | `true` | Отключить IPv6 через sysctl |

---

## Проверка после установки

### 1. Статусы systemd

```bash
systemctl is-active xray
systemctl is-active bridge-manager
systemctl is-active bridge-manager-tc
```

Ожидается: все нужные сервисы `active` (`bridge-manager-tc` только если `LIMITED_TC_ENABLED=true`).

### 2. Порты

```bash
ss -lntp | egrep '(:443|:1080|:10085|:8080)\s'
```

Ожидается:

- `*:443` -> xray
- `127.0.0.1:1080` -> xray (локальный socks-test)
- `127.0.0.1:10085` -> xray api inbound
- `127.0.0.1:8080` -> bridge-manager (если API_PUBLIC=false)

### 3. Health API

```bash
curl -s http://127.0.0.1:8080/health | jq .
curl -s http://127.0.0.1:8080/healthz
curl -s -H "Authorization: Bearer <API_TOKEN>" http://127.0.0.1:8080/v1/system/diagnostics | jq .
sudo /opt/bridge-manager/scripts/setup_tc.sh status
```

---

## REST API

### Базовая информация

**Базовый URL (локально):**
- `http://127.0.0.1:8080`

**Доступные эндпоинты:**
- `GET /health` — проверка состояния сервиса (без авторизации)
- `GET /healthz` — короткий plain-text healthcheck (без авторизации)
- `GET /v1/system/diagnostics` — effective transport/profile/firewall/tc статус (с авторизацией)
- `POST /v1/users` — создание/восстановление пользователя
- `GET /v1/users/{user_id}` — получение информации о пользователе
- `GET /v1/users/{user_id}/traffic` — получение статистики трафика пользователя
- `GET /v1/users/{user_id}/limit-policy` — получение политики ограничения трафика
- `PUT /v1/users/{user_id}/limit-policy` — установка/обновление политики ограничения трафика
- `DELETE /v1/users/{user_id}` — удаление (revoke) пользователя

**Авторизация:**
- Все эндпоинты `/v1/*` требуют HTTP-заголовок: `Authorization: Bearer <API_TOKEN>`
- Токен задаётся через переменную `API_TOKEN` при установке или в `/etc/bridge-manager/env`
- Эндпоинты `/health` и `/healthz` доступны без авторизации

---

### GET /health

**Описание:**  
Проверяет состояние сервиса и зависимостей (конфигурация Xray, сертификаты, активность процессов).

**Метод и путь:**
```
GET /health
```

**Заголовки:**  
Не требуются.

**Параметры:**  
Отсутствуют.

**Тело запроса:**  
Отсутствует.

**Пример запроса:**
```bash
curl -s http://127.0.0.1:8080/health | jq .
```

**Ответ при успехе (200 OK):**
```json
{
  "status": "ok",
  "checks": {
    "xray_config_exists": true,
    "xray_cert_exists": true,
    "xray_key_exists": true,
    "xray_active": true,
    "xray_listening_user_port": true
  }
}
```

**Описание полей ответа:**
- `status` (string): Общее состояние сервиса. Варианты: `"ok"`, `"degraded"`.
- `checks` (object): Детальные проверки компонентов:
  - `xray_config_exists` (boolean): Существует ли файл конфигурации Xray.
  - `xray_cert_exists` (boolean): Существует ли TLS-сертификат (для режима xhttp).
  - `xray_key_exists` (boolean): Существует ли приватный ключ (для режима xhttp).
  - `xray_active` (boolean): Активен ли systemd-сервис Xray.
  - `xray_listening_user_port` (boolean): Прослушивается ли порт для клиентских подключений.

**Ответ при деградации (503 Service Unavailable):**
```json
{
  "status": "degraded",
  "checks": {
    "xray_config_exists": true,
    "xray_cert_exists": true,
    "xray_key_exists": true,
    "xray_active": false,
    "xray_listening_user_port": false
  }
}
```

**Возможные ошибки:**
- `503 Service Unavailable`: Одна или несколько проверок провалились (см. `checks`).

---

### POST /v1/users

**Описание:**  
Создаёт нового пользователя с уникальным UUID или восстанавливает существующего (если он был удалён ранее). Если пользователь уже активен, возвращает его данные. При создании/восстановлении обновляет конфигурацию Xray (добавляет клиента в inbound).

**Метод и путь:**
```
POST /v1/users
```

**Заголовки:**
```
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

**Тело запроса:**
```json
{
  "user_id": "user123",
  "label": "User 123"
}
```

**Описание полей запроса:**
- `user_id` (string, обязательно): Уникальный идентификатор пользователя (1–128 символов). Используется как первичный ключ.
- `label` (string, необязательно): Человеко-читаемая метка для пользователя (до 255 символов). Используется в VLESS URI как fragment.

**Пример запроса:**
```bash
TOKEN='ваш_API_TOKEN'

curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user123","label":"User 123"}' | jq .
```

**Ответ при успехе (200 OK):**
```json
{
  "user_id": "user123",
  "uuid": "a1b2c3d4-e5f6-4789-8abc-def012345678",
  "label": "User 123",
  "created_at": "2026-02-17T10:30:45.123456+00:00",
  "revoked_at": null,
  "active": true,
  "vless_uri": "vless://a1b2c3d4-e5f6-4789-8abc-def012345678@test-bridge.example.com:443?encryption=none&security=tls&sni=test-bridge.example.com&type=xhttp&path=%2Fuser-xh#User%20123",
  "transport_mode": "xhttp"
}
```

**Описание полей ответа:**
- `user_id` (string): Идентификатор пользователя (тот же, что в запросе).
- `uuid` (string): UUID пользователя для протокола VLESS (генерируется автоматически).
- `label` (string | null): Метка пользователя.
- `created_at` (string): Дата и время создания пользователя (ISO 8601, UTC).
- `revoked_at` (string | null): Дата и время удаления пользователя. `null` если пользователь активен.
- `active` (boolean): `true` если пользователь активен, `false` если удалён (revoked).
- `vless_uri` (string): Готовая URI-строка для импорта в клиент (формат VLESS). Включает все параметры: адрес, порт, encryption, security, transport, path, SNI.
- `transport_mode` (string): Режим транспорта. Варианты: `"xhttp"`, `"reality"`.

**Дополнительные поля при transport_mode = "reality":**
```json
{
  "reality": {
    "server_name": "www.microsoft.com",
    "public_key": "abcd1234...",
    "short_id": "a1b2c3d4",
    "flow": "xtls-rprx-vision",
    "fingerprint": "chrome"
  }
}
```

**Описание полей reality:**
- `server_name` (string): Имя сервера для SNI в REALITY.
- `public_key` (string): Открытый ключ REALITY.
- `short_id` (string): Короткий идентификатор REALITY.
- `flow` (string): Режим flow для REALITY.
- `fingerprint` (string): Отпечаток TLS-клиента.

**Идемпотентность:**  
Повторный вызов с тем же `user_id` (если пользователь активен) вернёт существующего пользователя без изменений. Если пользователь был ранее удалён (revoked), он будет восстановлен с новым UUID.

**Возможные ошибки:**
- `401 Unauthorized`: Отсутствует или неверный токен в заголовке `Authorization`.
```json
{
  "detail": "Unauthorized"
}
```

- `500 Internal Server Error`: Ошибка при изменении конфигурации Xray или перезапуске сервиса.
```json
{
  "detail": "Failed to reload xray config: ..."
}
```

---

### GET /v1/users/{user_id}

**Описание:**  
Возвращает полную информацию о пользователе, включая UUID, метку, статус, VLESS URI и настройки транспорта.

**Метод и путь:**
```
GET /v1/users/{user_id}
```

**Параметры пути:**
- `user_id` (string): Идентификатор пользователя.

**Заголовки:**
```
Authorization: Bearer <API_TOKEN>
```

**Тело запроса:**  
Отсутствует.

**Пример запроса:**
```bash
TOKEN='ваш_API_TOKEN'

curl -s http://127.0.0.1:8080/v1/users/user123 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Ответ при успехе (200 OK):**
```json
{
  "user_id": "user123",
  "uuid": "a1b2c3d4-e5f6-4789-8abc-def012345678",
  "label": "User 123",
  "created_at": "2026-02-17T10:30:45.123456+00:00",
  "revoked_at": null,
  "active": true,
  "vless_uri": "vless://a1b2c3d4-e5f6-4789-8abc-def012345678@test-bridge.example.com:443?encryption=none&security=tls&sni=test-bridge.example.com&type=xhttp&path=%2Fuser-xh#User%20123",
  "transport_mode": "xhttp"
}
```

**Описание полей ответа:**  
Аналогично ответу `POST /v1/users` (см. выше).

**Возможные ошибки:**
- `401 Unauthorized`: Неверный или отсутствующий токен.
```json
{
  "detail": "Unauthorized"
}
```

- `404 Not Found`: Пользователь с указанным `user_id` не найден в базе данных.
```json
{
  "detail": "User not found"
}
```

---

### GET /v1/users/{user_id}/traffic

**Описание:**  
Возвращает статистику трафика пользователя: накопленные (сохранённые в БД) значения и текущие runtime-счётчики Xray. Runtime-счётчики сбрасываются при перезапуске Xray, но накопленные значения сохраняются постоянно.

**Метод и путь:**
```
GET /v1/users/{user_id}/traffic
```

**Параметры пути:**
- `user_id` (string): Идентификатор пользователя.

**Заголовки:**
```
Authorization: Bearer <API_TOKEN>
```

**Тело запроса:**  
Отсутствует.

**Пример запроса:**
```bash
TOKEN='ваш_API_TOKEN'

curl -s http://127.0.0.1:8080/v1/users/user123/traffic \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Ответ при успехе (200 OK):**
```json
{
  "user_id": "user123",
  "uplink_bytes": 1048576000,
  "downlink_bytes": 5242880000,
  "runtime_uplink_bytes": 524288,
  "runtime_downlink_bytes": 2097152
}
```

**Описание полей ответа:**
- `user_id` (string): Идентификатор пользователя.
- `uplink_bytes` (integer): Накопленный объём отправленных данных (upload) в байтах. Сохраняется в БД, не сбрасывается при перезапуске Xray.
- `downlink_bytes` (integer): Накопленный объём полученных данных (download) в байтах. Сохраняется в БД, не сбрасывается при перезапуске Xray.
- `runtime_uplink_bytes` (integer): Текущий runtime-счётчик отправленных данных (upload) в байтах. Показывает трафик с момента последнего запуска Xray. Сбрасывается в 0 при рестарте Xray.
- `runtime_downlink_bytes` (integer): Текущий runtime-счётчик полученных данных (download) в байтах. Показывает трафик с момента последнего запуска Xray. Сбрасывается в 0 при рестарте Xray.

**Механизм работы:**  
Фоновый процесс (TrafficCollector) каждые 15 секунд опрашивает Xray API и сохраняет дельту трафика в БД. При запросе `/traffic` API возвращает:
- Актуальные runtime-значения из Xray (снимок в момент запроса).
- Накопленные totals из БД (с учётом текущей дельты).

После рестарта Xray:
- `runtime_uplink_bytes` и `runtime_downlink_bytes` становятся `0` или малыми значениями.
- `uplink_bytes` и `downlink_bytes` продолжают накапливаться и не теряются.

**Возможные ошибки:**
- `401 Unauthorized`: Неверный или отсутствующий токен.
```json
{
  "detail": "Unauthorized"
}
```

- `404 Not Found`: Пользователь с указанным `user_id` не найден в БД.
```json
{
  "detail": "User not found"
}
```

**Примечания:**
- Если Xray API недоступен, возвращаются только накопленные значения из БД, а runtime-поля будут `0`.
- Для конвертации байт в удобные единицы: `1 МБ = 1048576 байт`, `1 ГБ = 1073741824 байт`.

---

### DELETE /v1/users/{user_id}

**Описание:**  
Удаляет (revoke) пользователя: помечает его как неактивного в БД и удаляет из конфигурации Xray (inbound clients). Пользователь больше не сможет подключаться. Удалённый пользователь может быть восстановлен через `POST /v1/users` (получит новый UUID).

**Метод и путь:**
```
DELETE /v1/users/{user_id}
```

**Параметры пути:**
- `user_id` (string): Идентификатор пользователя.

**Заголовки:**
```
Authorization: Bearer <API_TOKEN>
```

**Тело запроса:**  
Отсутствует.

**Пример запроса:**
```bash
TOKEN='ваш_API_TOKEN'

curl -s -X DELETE http://127.0.0.1:8080/v1/users/user123 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Ответ при успехе (200 OK):**
```json
{
  "status": "deleted",
  "user_id": "user123",
  "removed_from_xray": true
}
```

**Описание полей ответа:**
- `status` (string): Статус операции. Всегда `"deleted"` при успехе.
- `user_id` (string): Идентификатор удалённого пользователя.
- `removed_from_xray` (boolean): `true` если пользователь был найден и удалён из конфигурации Xray. `false` если пользователь не был найден в конфигурации (уже удалён ранее или не добавлялся).

**Побочные эффекты:**
- Запись в БД помечается: `revoked_at = <текущее время UTC>`.
- Клиент удаляется из inbound `inbound-from-users` в `/usr/local/etc/xray/config.json`.
- Xray перезагружается (либо через systemd restart, либо через API, в зависимости от настроек).
- Накопленная статистика трафика (`uplink_bytes`, `downlink_bytes`) сохраняется в БД и доступна для анализа.

**Возможные ошибки:**
- `401 Unauthorized`: Неверный или отсутствующий токен.
```json
{
  "detail": "Unauthorized"
}
```

- `404 Not Found`: Пользователь не найден или уже был удалён ранее.
```json
{
  "detail": "User not found"
}
```

- `500 Internal Server Error`: Ошибка при изменении конфигурации Xray.
```json
{
  "detail": "Failed to remove user from xray config: ..."
}
```

---

### Примеры использования API

#### Полный цикл работы с пользователем

```bash
# 1. Установить токен
export TOKEN='your_secret_api_token_here'

# 2. Проверить здоровье сервиса
curl -s http://127.0.0.1:8080/health | jq .

# 3. Создать пользователя
curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","label":"Alice Smith"}' | jq .

# Ответ: получите vless_uri, которую можно импортировать в клиент

# 4. Получить информацию о пользователе
curl -s http://127.0.0.1:8080/v1/users/alice \
  -H "Authorization: Bearer $TOKEN" | jq .

# 5. Проверить статистику трафика
curl -s http://127.0.0.1:8080/v1/users/alice/traffic \
  -H "Authorization: Bearer $TOKEN" | jq .

# Ответ:
# {
#   "user_id": "alice",
#   "uplink_bytes": 0,
#   "downlink_bytes": 0,
#   "runtime_uplink_bytes": 0,
#   "runtime_downlink_bytes": 0
# }

# 6. После использования клиентом — проверить снова
curl -s http://127.0.0.1:8080/v1/users/alice/traffic \
  -H "Authorization: Bearer $TOKEN" | jq .

# Ответ:
# {
#   "user_id": "alice",
#   "uplink_bytes": 52428800,
#   "downlink_bytes": 524288000,
#   "runtime_uplink_bytes": 52428800,
#   "runtime_downlink_bytes": 524288000
# }

# 7. Удалить пользователя
curl -s -X DELETE http://127.0.0.1:8080/v1/users/alice \
  -H "Authorization: Bearer $TOKEN" | jq .

# Ответ:
# {
#   "status": "deleted",
#   "user_id": "alice",
#   "removed_from_xray": true
# }

# 8. Попытка получить удалённого пользователя вернёт 404
curl -s http://127.0.0.1:8080/v1/users/alice \
  -H "Authorization: Bearer $TOKEN" | jq .

# Ответ:
# {
#   "detail": "User not found"
# }

# 9. Восстановление пользователя (получит новый UUID)
curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice","label":"Alice Smith (restored)"}' | jq .
```

#### Массовое создание пользователей

```bash
export TOKEN='your_secret_api_token_here'

for i in {1..10}; do
  curl -s -X POST http://127.0.0.1:8080/v1/users \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"user$i\",\"label\":\"User $i\"}" | jq -r '.vless_uri'
done
```

#### Мониторинг трафика

```bash
export TOKEN='your_secret_api_token_here'

# Получить трафик для конкретного пользователя
curl -s http://127.0.0.1:8080/v1/users/alice/traffic \
  -H "Authorization: Bearer $TOKEN" | \
  jq '{user: .user_id, total_gb: ((.uplink_bytes + .downlink_bytes) / 1073741824 | round)}'

# Пример вывода:
# {
#   "user": "alice",
#   "total_gb": 5
# }

---

## Ограничение трафика (Traffic Limit Policy)

### Обзор

Каждому пользователю можно назначить политику ограничения трафика. Два режима:

| Режим | Поведение |
|---|---|
| **Unlimited** (по умолчанию) | Без ограничений по объёму и скорости |
| **Limited** | Лимит по суммарному трафику (uplink + downlink), после достижения: `throttle` (замедление) или `block` (полная блокировка) |

### Поля политики

| Поле | Тип | Описание |
|---|---|---|
| `mode` | `"unlimited"` / `"limited"` | Режим |
| `traffic_limit_bytes` | integer / null | Лимит в байтах (обязателен для limited) |
| `post_limit_action` | `"throttle"` / `"block"` / null | Действие после достижения лимита |
| `throttle_rate_bytes_per_sec` | integer / null | Скорость при throttle (дефолт: 102400 = 100 KB/s) |
| `enforcement_state` | `"none"` / `"throttled"` / `"blocked"` | Текущее состояние enforcement |
| `limit_reached_at` | datetime / null | Когда лимит был достигнут |
| `total_bytes_observed` | integer | Текущий суммарный трафик |

### Throttle vs Block

- **Throttle**: пользователь остаётся подключаемым, но скорость ограничивается до заданного значения (по умолчанию 100 KB/s = 102400 B/s). Реализовано через tc + fwmark на уровне Linux kernel.
- **Block**: трафик обнуляется полностью через routing в blackhole outbound Xray. Пользователь не удаляется, UUID не меняется — только маршрут трафика.

### Точность лимита

Из-за polling-based архитектуры (каждые 15 секунд) точный hard-cut по байту невозможен. Возможен перелёт лимита на объём трафика за один интервал опроса. Это нормальное ожидаемое ограничение системы.

---

### GET /v1/users/{user_id}/limit-policy

Возвращает текущую политику ограничения трафика пользователя.

```bash
curl -s http://127.0.0.1:8080/v1/users/alice/limit-policy \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Ответ:
```json
{
  "user_id": "alice",
  "mode": "unlimited",
  "traffic_limit_bytes": null,
  "post_limit_action": null,
  "throttle_rate_bytes_per_sec": null,
  "enforcement_state": "none",
  "limit_reached_at": null,
  "total_bytes_observed": 0
}
```

---

### PUT /v1/users/{user_id}/limit-policy

Устанавливает или обновляет политику.

**Unlimited** (снять ограничения):
```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/alice/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"unlimited"}' | jq .
```

**Limited + throttle** (10 GB, потом 100 KB/s):
```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/alice/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"limited","traffic_limit_bytes":10737418240,"post_limit_action":"throttle","throttle_rate_bytes_per_sec":102400}' | jq .
```

**Limited + block** (5 GB, потом полная блокировка):
```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/alice/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"limited","traffic_limit_bytes":5368709120,"post_limit_action":"block"}' | jq .
```

**Правила валидации:**
- `mode=unlimited`: `traffic_limit_bytes`, `post_limit_action`, `throttle_rate_bytes_per_sec` игнорируются и сбрасываются; `enforcement_state` сбрасывается в `"none"`.
- `mode=limited`: `traffic_limit_bytes` обязателен и > 0; `post_limit_action` обязателен.
- `post_limit_action=throttle`: `throttle_rate_bytes_per_sec` обязателен (дефолт 102400).
- Некорректные комбинации возвращают 422.

---

### Переменные окружения для Traffic Limiting

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LIMITED_THROTTLE_RATE_BYTES_PER_SEC` | `102400` | Дефолтная скорость throttle (100 KB/s) |
| `LIMITED_TC_ENABLED` | `true` | Включить tc shaping при bootstrap |
| `LIMITED_TC_EGRESS_IFACE` | *(авто)* | Егрес-интерфейс (auto-detect если пусто) |
| `LIMITED_TC_MARK` | `100` | fwmark для throttled пакетов |
| `LIMITED_TC_CLASS_ID` | `1:10` | tc class id для throttled трафика |
| `LIMIT_POLL_INTERVAL_SECONDS` | `15` | Интервал enforcement loop |

---

### Troubleshooting

**Проверить tc qdisc:**
```bash
tc -s qdisc show dev $(ip route show default | awk '/default/{print $5}')
tc -s class show dev $(ip route show default | awk '/default/{print $5}')
tc -s filter show dev $(ip route show default | awk '/default/{print $5}')
```

**Сбросить tc:**
```bash
sudo ./scripts/setup_tc.sh teardown
```

**Переприменить tc:**
```bash
sudo LIMITED_THROTTLE_RATE_BYTES_PER_SEC=102400 ./scripts/setup_tc.sh
```

**Проверить enforcement rules в Xray config:**
```bash
jq '.routing.rules[] | select(.attrs._enforcement)' /usr/local/etc/xray/config.json
```

**Проверить throttle outbound:**
```bash
jq '.outbounds[] | select(.tag=="to-exit-throttled")' /usr/local/etc/xray/config.json
```

**Логи enforcement:**
```bash
journalctl -u bridge-manager --no-pager | grep -E 'limit_reached|throttle_applied|block_applied|enforcement_cleared|policy_changed'
```

---

## Проверка цепочки Bridge -> Exit -> Internet

### Доступность exit

```bash
timeout 3 bash -c "echo > /dev/tcp/s1.bytestand.fun/443"
```

### Локальный socks smoke

```bash
curl --socks5 127.0.0.1:1080 -s https://ifconfig.me
curl --socks5-hostname 127.0.0.1:1080 -s https://api4.ipify.org
```

Для IPv4-проверки используйте `api4.ipify.org`.

---

## SSH-туннель к API (рекомендуется при API_PUBLIC=false)

На вашей локальной машине:

```bash
ssh -L 8080:127.0.0.1:8080 root@<BRIDGE_SERVER_IP>
```

После этого локально откроется API bridge:

- `http://127.0.0.1:8080/docs`

---

## Как открыть API наружу безопаснее

Вариант 1 (лучше): оставить `API_PUBLIC=false`, использовать SSH tunnel.

Вариант 2: открыть наружу.

1. В `/etc/bridge-manager/env` поставить `API_BIND=0.0.0.0`.
2. В firewall лучше открыть 8080 только для вашего IP:

```bash
ufw allow from <YOUR_PUBLIC_IP> to any port 8080 proto tcp
```

3. Перезапустить сервис:

```bash
systemctl restart bridge-manager
```

Не рекомендуется открывать `8080/tcp` для всех без IP-ограничений.

---

## Структура проекта

```text
/opt/bridge-manager/
  app/
    main.py
    storage.py
    xray_config.py
    stats.py
    models.py
    auth.py
    settings.py
    enforcement.py
  scripts/
    bootstrap_bridge.sh
    setup_tc.sh
    smoke.sh
  tests/
    test_api.py
    test_stats.py
    test_xray_config.py
    test_enforcement.py
    helpers.py
  deploy/
    env.example
  requirements.txt
  README.md
  IMPLEMENTATION_REPORT.md
```

---

## Важные пути в системе

- Xray binary: `/usr/local/bin/xray`
- Xray config: `/usr/local/etc/xray/config.json`
- Xray cert/key:
- `/usr/local/etc/xray/fullchain.crt`
- `/usr/local/etc/xray/private.key`
- Xray unit: `/etc/systemd/system/xray.service`
- Manager env: `/etc/bridge-manager/env`
- Manager unit: `/etc/systemd/system/bridge-manager.service`
- SQLite DB: `/opt/bridge-manager/data/bridge_manager.db`

---

## Логи и диагностика

```bash
journalctl -u xray -f
journalctl -u bridge-manager -f
systemctl status xray --no-pager
systemctl status bridge-manager --no-pager
```

Проверка валидности Xray-конфига:

```bash
/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
```

---

## Идемпотентность API

- Повторный `POST /v1/users` с тем же `user_id` (если не revoked) возвращает существующий UUID/URI.
- `DELETE` помечает пользователя как revoked и удаляет из Xray `clients` inbound `inbound-from-users`.

---

## Безопасность

1. Не храните `API_TOKEN` в публичных репозиториях.
2. Не публикуйте `/etc/bridge-manager/env`.
3. Не пушьте в git рабочие БД (`/opt/bridge-manager/data/*`).
4. Не публикуйте приватные TLS ключи.

---

## Лицензия

Используйте внутри вашей инфраструктуры по вашим правилам.
