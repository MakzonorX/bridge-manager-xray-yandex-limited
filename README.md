# Bridge Manager + Xray Bridge (RU)

Готовое решение для схемы:

1. Клиент -> Bridge: `VLESS + XHTTP + TLS` на `:443`, `path=/user-xh`
2. Bridge -> Exit: `VLESS + XHTTP + TLS` на `s1.bytestand.fun:443`, `path=/bridge-xh`
3. Bridge Manager (FastAPI): выпуск/удаление ключей, хранение в SQLite, управление users в inbound Xray.

Важно:
- На exit-node ничего не меняется.
- Xray работает на хосте через systemd (не docker).
- REST API по умолчанию слушает только `127.0.0.1:8080`.

---

## Что уже зашито в проекте (константы exit)

- `EXIT_HOST = s1.bytestand.fun`
- `EXIT_PORT = 443`
- `EXIT_TRANSPORT = xhttp`
- `EXIT_PATH = /bridge-xh`
- `BRIDGE_UUID_FOR_EXIT = 7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0`
- `EXIT_TLS = true`, `serverName = s1.bytestand.fun`

---

## Требования перед деплоем на чистый сервер

1. Ubuntu 24.04 (root/sudo доступ).
2. Домен bridge (например `test-bridge.example.com`) уже указывает A-записью на IP этого сервера.
3. Порты открыты снаружи:
- `22/tcp`
- `80/tcp` (для выпуска сертификата)
- `443/tcp`

Если API хотите открыть наружу, потребуется также `8080/tcp`.

---

## Быстрый запуск (чистый сервер)

### 1. Клонировать репозиторий

```bash
sudo git clone <PRIVATE_REPO_URL> /opt/bridge-manager
cd /opt/bridge-manager
```

### 2. Запустить bootstrap

```bash
sudo BRIDGE_DOMAIN=bridge.example.com \
ACME_EMAIL=admin@example.com \
API_TOKEN='очень_длинный_секрет' \
API_PUBLIC=false \
./scripts/bootstrap_bridge.sh
```

### 3. Что делает bootstrap

Скрипт автоматически:

1. Ставит зависимости (`curl`, `ufw`, `python3-venv`, и т.д.).
2. Включает time sync.
3. Настраивает firewall (`22/80/443`, а `8080` только если `API_PUBLIC=true`).
4. Ставит Xray `v26.2.6` в `/usr/local/bin/xray`.
5. Выпускает Let's Encrypt сертификат через `acme.sh` (standalone).
6. Пишет Xray-конфиг bridge в `/usr/local/etc/xray/config.json`.
7. Поднимает `xray.service`.
8. Ставит Python venv и зависимости Bridge Manager.
9. Пишет env-файл `/etc/bridge-manager/env`.
10. Поднимает `bridge-manager.service`.

---

## Переменные bootstrap

Обязательные:

- `BRIDGE_DOMAIN`
- `ACME_EMAIL`
- `API_TOKEN`

Опциональные:

- `API_PUBLIC=false|true` (по умолчанию `false`)
- `DISABLE_IPV6=true|false` (по умолчанию `true`)

Пример:

```bash
sudo BRIDGE_DOMAIN=test-bridge.example.com \
ACME_EMAIL=admin@example.com \
API_TOKEN='supersecret' \
API_PUBLIC=false \
DISABLE_IPV6=true \
./scripts/bootstrap_bridge.sh
```

---

## Проверка после установки

### 1. Статусы systemd

```bash
systemctl is-active xray
systemctl is-active bridge-manager
```

Ожидается: оба `active`.

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
```

---

## REST API

Базовый URL (локально):

- `http://127.0.0.1:8080`

Эндпоинты:

- `GET /health`
- `POST /v1/users`
- `GET /v1/users/{user_id}`
- `GET /v1/users/{user_id}/traffic`
- `DELETE /v1/users/{user_id}`

Авторизация:

- Все `/v1/*` требуют `Authorization: Bearer <API_TOKEN>`.
- `/health` открытый.

### POST /v1/users

Запрос:

```bash
TOKEN='ваш_API_TOKEN'

curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user123","label":"User 123"}' | jq .
```

Ответ содержит `vless_uri` формата:

```text
vless://{UUID}@{BRIDGE_DOMAIN}:443?encryption=none&security=tls&sni={BRIDGE_DOMAIN}&type=xhttp&path=%2Fuser-xh#{label_or_userid}
```

### GET /v1/users/{user_id}

```bash
curl -s http://127.0.0.1:8080/v1/users/user123 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### GET /v1/users/{user_id}/traffic

```bash
curl -s http://127.0.0.1:8080/v1/users/user123/traffic \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Если статистика недоступна, endpoint не падает и возвращает `0/0`.

### DELETE /v1/users/{user_id}

```bash
curl -s -X DELETE http://127.0.0.1:8080/v1/users/user123 \
  -H "Authorization: Bearer $TOKEN" | jq .
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
  scripts/
    bootstrap_bridge.sh
    smoke.sh
  deploy/
    env.example
  requirements.txt
  README.md
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
