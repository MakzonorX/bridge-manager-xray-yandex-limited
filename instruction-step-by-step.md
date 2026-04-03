# Instruction Step by Step: Bridge Manager + Xray Bridge

Документ для развёртывания с нуля на новом сервере Ubuntu 24.04.

Формат: строго пошаговый, чтобы можно было пройти от чистого сервера до рабочего выпуска ключей.

---

## 0) Что вы получите в итоге

После выполнения всех шагов у вас будет:

1. Xray на хосте (systemd), слушает `443/tcp`.
2. Цепочка `Client -> Bridge -> Exit` работает.
3. Bridge Manager API работает (`FastAPI`, `SQLite`, Bearer token).
4. Эндпоинты выпуска и удаления ключей доступны:
- `POST /v1/users`
- `GET /v1/users/{user_id}`
- `GET /v1/users/{user_id}/traffic`
- `DELETE /v1/users/{user_id}`
- `GET /health`
5. **Ограничение трафика по ключу**: для каждого пользователя можно задать лимит трафика.
- `GET /v1/users/{user_id}/limit-policy` — текущая политика
- `PUT /v1/users/{user_id}/limit-policy` — установить/обновить политику
- Два режима: `unlimited` (без ограничений) и `limited` (лимит в байтах)
- Две политики после достижения лимита: `throttle` (замедление до 100 KB/s) или `block` (полная блокировка)
- Enforcement работает автоматически через фоновый цикл + tc shaping + Xray routing

---

## 1) Данные, которые подготовить заранее

Обязательные параметры:

1. `BRIDGE_DOMAIN`
 - Пример: `test-bridge.example.com`
 - Требование: A-запись этого домена уже указывает на публичный IP нового сервера.

2. `API_TOKEN`
 - Длинный секрет для `Authorization: Bearer <token>`.

3. `ACME_EMAIL` (только если `USER_MODE=xhttp`)
 - Email для выпуска TLS сертификата через Let's Encrypt/acme.sh.
 - При `USER_MODE=reality` не нужен.

Параметр протокола (клиент → bridge):

4. `USER_MODE`
 - `reality` (по умолчанию): VLESS + REALITY. Сертификат на bridge не нужен.
 - `xhttp`: VLESS + XHTTP + TLS. Требует `ACME_EMAIL` и открытый `80/tcp`.

Параметры exit-ноды (bridge → exit):

5. `EXIT_HOST` — хост exit-ноды (дефолт: `s1.bytestand.fun`)
6. `EXIT_PORT` — порт exit-ноды (дефолт: `443`)
7. `EXIT_PATH` — XHTTP path на exit (дефолт: `/bridge-xh`)
8. `EXIT_SERVER_NAME` — TLS SNI (дефолт: равен `EXIT_HOST`)
9. `BRIDGE_UUID_FOR_EXIT` — UUID этого bridge-клиента на exit-ноде (дефолт: built-in)

Прочее опциональное:

10. `API_PUBLIC`

---

## 2) Проверка нового сервера (минимум)

Под root или через sudo выполните:

```bash
hostnamectl --static
uname -a
ip -4 addr show scope global
```

Проверьте DNS домена:

```bash
getent hosts <BRIDGE_DOMAIN>
dig +short A <BRIDGE_DOMAIN>
```

Ожидание:
- Оба запроса возвращают IP именно этого нового сервера.

Если не совпадает, остановитесь и исправьте DNS.

---

## 3) Клонирование репозитория

Рекомендуемый путь:

```bash
sudo git clone https://github.com/MakzonorX/bridge-manager-xray.git /opt/bridge-manager
cd /opt/bridge-manager
```

Проверьте, что скрипт существует:

```bash
ls -l scripts/bootstrap_bridge.sh
```

---

## 4) Запуск bootstrap (основной шаг)

### Вариант A: USER_MODE=reality (рекомендуется)

Не требует ACME_EMAIL и открытого `80/tcp`. Сертификат на bridge не выпускается.

**Минимальный запуск:**

```bash
sudo BRIDGE_DOMAIN='<ВАШ_ДОМЕН>' \
API_TOKEN='<ВАШ_ТОКЕН>' \
./scripts/bootstrap_bridge.sh
```

**Полный запуск со всеми параметрами (кастомный exit, API наружу):**

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
API_TOKEN='super_secret_token_value' \
API_PUBLIC=true \
USER_MODE=reality \
USER_PORT=443 \
USER_FLOW=xtls-rprx-vision \
REALITY_SERVER_NAME='ads.x5.ru' \
EXIT_HOST='s1.bytestand.fun' \
EXIT_PORT=443 \
EXIT_PATH='/bridge-xh' \
EXIT_SERVER_NAME='s1.bytestand.fun' \
BRIDGE_UUID_FOR_EXIT='7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0' \
DISABLE_IPV6=true \
./scripts/bootstrap_bridge.sh
```

---

### Вариант B: USER_MODE=xhttp

Клиент подключается по VLESS + XHTTP + TLS. Требуется `ACME_EMAIL` и доступный снаружи `80/tcp` для выпуска сертификата.

**Полный запуск со всеми параметрами (кастомный exit, API наружу):**

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
ACME_EMAIL='admin@example.com' \
API_TOKEN='super_secret_token_value' \
API_PUBLIC=true \
USER_MODE=xhttp \
USER_PORT=443 \
USER_PATH='/user-xh' \
EXIT_HOST='s1.bytestand.fun' \
EXIT_PORT=443 \
EXIT_PATH='/bridge-xh' \
EXIT_SERVER_NAME='s1.bytestand.fun' \
BRIDGE_UUID_FOR_EXIT='7d28c9a1-e5f3-4b90-8a2f-d3e4b7c9f8a0' \
DISABLE_IPV6=true \
./scripts/bootstrap_bridge.sh
```

---

### Что делает скрипт автоматически

1. Устанавливает системные пакеты.
2. Включает синхронизацию времени.
3. Настраивает UFW (`22`, `80`, `443`, и `8080` если `API_PUBLIC=true`).
4. Ставит Xray `v26.2.6`.
5. *(Только `USER_MODE=xhttp`)* Выпускает TLS-сертификат через acme.sh standalone (`:80`).
6. Пишет Xray-конфиг bridge (в соответствии с выбранным режимом).
7. Включает и стартует `xray.service`.
8. Создаёт Python venv, ставит зависимости Bridge Manager.
9. Пишет `/etc/bridge-manager/env`.
10. Включает и стартует `bridge-manager.service`.

---

## 5) Первичная проверка сервисов

Проверьте статус:

```bash
systemctl is-active xray
systemctl is-active bridge-manager
```

Оба должны вернуть `active`.

Проверьте порты:

```bash
ss -lntp | egrep '(:443|:1080|:10085|:8080)\s'
```

Ожидание:

1. `*:443` -> процесс `xray`
2. `127.0.0.1:1080` -> `xray` (локальный socks-test)
3. `127.0.0.1:10085` -> `xray` (api inbound)
4. `127.0.0.1:8080` -> `uvicorn` (если `API_PUBLIC=false`)

---

## 6) Проверка TLS на bridge-домене

```bash
echo | openssl s_client -connect <BRIDGE_DOMAIN>:443 -servername <BRIDGE_DOMAIN> 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

Ожидание:
- `subject=CN = <BRIDGE_DOMAIN>`
- issuer Let’s Encrypt
- срок действия валиден

---

## 7) Проверка API

### 7.1 Health

```bash
curl -s http://127.0.0.1:8080/health | jq .
```

Ожидание:
- `status: "ok"`

### 7.2 Создать пользователя

```bash
TOKEN='<ВАШ_API_TOKEN>'

curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo-user-1","label":"Demo User 1"}' | jq .
```

Ожидание:
- В ответе есть `uuid` и `vless_uri`.
- Формат URI:

```text
vless://UUID@BRIDGE_DOMAIN:443?encryption=none&security=tls&sni=BRIDGE_DOMAIN&type=xhttp&path=%2Fuser-xh#label
```

### 7.3 Получить пользователя

```bash
curl -s http://127.0.0.1:8080/v1/users/demo-user-1 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### 7.4 Проверка трафика

```bash
curl -s http://127.0.0.1:8080/v1/users/demo-user-1/traffic \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Ответ содержит:

- `uplink_bytes`, `downlink_bytes` — накопленный трафик (persisted в SQLite).
- `runtime_uplink_bytes`, `runtime_downlink_bytes` — текущие runtime-счётчики Xray.

Важно:
- после рестарта Xray runtime-поля могут быть `0`,
- но накопленные `uplink_bytes/downlink_bytes` должны сохраняться.

### 7.5 Удалить пользователя

```bash
curl -s -X DELETE http://127.0.0.1:8080/v1/users/demo-user-1 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

### 7.6 Ограничение трафика: получить текущую политику

```bash
curl -s http://127.0.0.1:8080/v1/users/demo-user-1/limit-policy \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Ответ по умолчанию (для нового пользователя):

```json
{
  "user_id": "demo-user-1",
  "mode": "unlimited",
  "traffic_limit_bytes": null,
  "post_limit_action": null,
  "throttle_rate_bytes_per_sec": null,
  "enforcement_state": "none",
  "limit_reached_at": null,
  "total_bytes_observed": 0
}
```

Важные поля:
- `mode`: `"unlimited"` — нет ограничений; `"limited"` — лимит задан.
- `enforcement_state`: `"none"` — ограничение не применено; `"throttled"` — скорость снижена; `"blocked"` — трафик обнулён.
- `total_bytes_observed`: суммарный трафик пользователя (uplink + downlink) в байтах.

### 7.7 Ограничение трафика: установить политику

**Пример 1: Лимит 10 GB + замедление до 100 KB/s после достижения**

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/demo-user-1/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"limited","traffic_limit_bytes":10737418240,"post_limit_action":"throttle","throttle_rate_bytes_per_sec":102400}' | jq .
```

**Пример 2: Лимит 5 GB + полная блокировка после достижения**

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/demo-user-1/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"limited","traffic_limit_bytes":5368709120,"post_limit_action":"block"}' | jq .
```

**Пример 3: Снять ограничения (вернуть unlimited)**

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/users/demo-user-1/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"unlimited"}' | jq .
```

**Как это работает:**

1. Вы задаёте `mode=limited` с лимитом (`traffic_limit_bytes`) и действием (`post_limit_action`).
2. Фоновый процесс каждые 15 секунд проверяет суммарный трафик пользователя (uplink + downlink).
3. Когда `total_bytes >= traffic_limit_bytes`, срабатывает enforcement:
   - **throttle**: Xray перенаправляет трафик пользователя в отдельный outbound. Linux tc ограничивает скорость этого outbound до заданного значения (по умолчанию 102400 B/s = 100 KB/s). Пользователь остаётся подключаемым, но медленно.
   - **block**: Xray перенаправляет трафик в blackhole outbound. Пользователь не может передавать данные вообще. UUID и ключ НЕ удаляются.
4. После изменения enforcement Xray перезапускается (systemctl restart).
5. Enforcement переживает рестарты bridge-manager и Xray — состояние хранится в SQLite.
6. Для снятия: отправьте `PUT` с `{"mode":"unlimited"}`. Enforcement сбрасывается, routing/tc правила убираются.

**Точность лимита:**
Лимит проверяется с интервалом ~15 секунд. Перелёт на объём трафика за один интервал возможен и является ожидаемым ограничением.

---

## 8) Проверка, что user реально попадает в Xray

После POST:

```bash
cat /usr/local/etc/xray/config.json | jq '.inbounds[] | select(.tag=="inbound-from-users") | .settings.clients'
```

Ожидание:
- В списке есть `email: "user:<user_id>"` и соответствующий `id`.

После DELETE:
- Соответствующая запись удалена из `clients`.

---

## 9) Проверка цепочки Bridge -> Exit -> Internet

Проверка доступности exit:

```bash
timeout 3 bash -c "echo > /dev/tcp/s1.bytestand.fun/443"
```

Проверка через локальный socks-test:

```bash
curl --socks5 127.0.0.1:1080 -s https://ifconfig.me
curl --socks5-hostname 127.0.0.1:1080 -s https://api4.ipify.org
```

Примечание:
- `ifconfig.me` может показать IPv6 выход.
- Для строгой IPv4 проверки используйте `api4.ipify.org`.

---

## 10) Как пользоваться API безопасно (без открытия наружу)

Рекомендуемый режим:
- Оставить `API_PUBLIC=false`.
- Работать через SSH туннель.

На локальной машине:

```bash
ssh -L 8080:127.0.0.1:8080 root@<SERVER_IP>
```

После этого локально открывайте:

- `http://127.0.0.1:8080/docs`

---

## 11) Если нужно открыть API наружу

1. Измените `/etc/bridge-manager/env`:

```bash
API_BIND=0.0.0.0
```

2. Ограничьте доступ firewall только вашим IP:

```bash
ufw allow from <YOUR_PUBLIC_IP> to any port 8080 proto tcp
```

3. Перезапустите:

```bash
systemctl restart bridge-manager
```

Не открывайте 8080 для всех без IP-ограничения.

---

## 12) Логи и диагностика

```bash
journalctl -u xray -f
journalctl -u bridge-manager -f
```

Статус сервисов:

```bash
systemctl status xray --no-pager
systemctl status bridge-manager --no-pager
```

Проверка валидности Xray конфигурации:

```bash
/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
```

Логи enforcement (ограничение трафика):

```bash
journalctl -u bridge-manager --no-pager | grep -E 'policy_changed|limit_reached|throttle_applied|block_applied|enforcement_cleared|xray_reload'
```

Проверка tc shaping:

```bash
tc -s qdisc show dev $(ip route show default | awk '/default/{print $5}')
tc -s class show dev $(ip route show default | awk '/default/{print $5}')
```

Проверка enforcement rules в Xray config:

```bash
jq '.routing.rules[] | select(.attrs._enforcement)' /usr/local/etc/xray/config.json
jq '.outbounds[] | select(.tag=="to-exit-throttled")' /usr/local/etc/xray/config.json
```

---

## 13) Что делать при типовых проблемах

### Проблема: сертификат не выпускается

Проверьте:

1. DNS A-запись домена указывает на этот сервер.
2. `80/tcp` доступен снаружи.
3. Nginx/Apache не заняли `:80` во время выпуска.

### Проблема: API отвечает 401

Проверьте заголовок:

```bash
Authorization: Bearer <API_TOKEN>
```

и значение `API_TOKEN` в `/etc/bridge-manager/env`.

### Проблема: POST /v1/users возвращает 500

Проверьте:

1. Логи `bridge-manager`.
2. Валидацию Xray-конфига (`xray run -test ...`).
3. Статус `xray.service`.

### Проблема: throttle не работает (скорость не ограничивается)

Проверьте:

1. Установлен ли iproute2: `which tc`
2. Настроен ли tc: `tc -s qdisc show dev <iface>` — должен быть htb qdisc.
3. Есть ли в Xray config outbound `to-exit-throttled`: `jq '.outbounds[].tag' /usr/local/etc/xray/config.json`
4. Есть ли routing rules enforcement: `jq '.routing.rules[] | select(.attrs._enforcement)' /usr/local/etc/xray/config.json`
5. Значение fwmark совпадает: `LIMITED_TC_MARK` в `/etc/bridge-manager/env` и `sockopt.mark` в Xray outbound.

Переприменить tc:

```bash
sudo LIMITED_THROTTLE_RATE_BYTES_PER_SEC=102400 /opt/bridge-manager/scripts/setup_tc.sh
```

### Проблема: enforcement_state не меняется

Проверьте:

1. Политика задана правильно: `curl .../limit-policy` — `mode` должен быть `limited`.
2. Трафик достиг лимита: `total_bytes_observed >= traffic_limit_bytes`.
3. Enrollment loop работает: в логах bridge-manager должны быть записи `limit_reached`.

### Проблема: пользователь заблокирован, нужно разблокировать

```bash
curl -X PUT http://127.0.0.1:8080/v1/users/<user_id>/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"unlimited"}'
```

Это сбросит `enforcement_state` в `"none"`, уберёт routing rules и снимет throttle/block.

---

## 14) Обновление на сервере

Если в репозитории появились изменения:

```bash
cd /opt/bridge-manager
git pull
```

Затем повторно запустите bootstrap с теми же параметрами, с которыми сервер был установлен изначально. Скрипт идемпотентен — переустановит/актуализирует состояние.

Пример для `USER_MODE=reality`:

```bash
sudo BRIDGE_DOMAIN='<ВАШ_ДОМЕН>' \
API_TOKEN='<ВАШ_API_TOKEN>' \
API_PUBLIC=false \
USER_MODE=reality \
EXIT_HOST='<EXIT_HOST>' \
EXIT_PORT=443 \
EXIT_PATH='<EXIT_PATH>' \
BRIDGE_UUID_FOR_EXIT='<UUID>' \
./scripts/bootstrap_bridge.sh
```

Пример для `USER_MODE=xhttp`:

```bash
sudo BRIDGE_DOMAIN='<ВАШ_ДОМЕН>' \
ACME_EMAIL='<ВАШ_EMAIL>' \
API_TOKEN='<ВАШ_API_TOKEN>' \
API_PUBLIC=false \
USER_MODE=xhttp \
EXIT_HOST='<EXIT_HOST>' \
EXIT_PORT=443 \
EXIT_PATH='<EXIT_PATH>' \
BRIDGE_UUID_FOR_EXIT='<UUID>' \
./scripts/bootstrap_bridge.sh
```

---

## 15) Минимальный чеклист "готово к работе"

1. `systemctl is-active xray` -> `active`
2. `systemctl is-active bridge-manager` -> `active`
3. `curl http://127.0.0.1:8080/health` -> `status=ok`
4. `POST /v1/users` выдаёт валидный `vless_uri`
5. Пользователь появляется в `clients` inbound `inbound-from-users`
6. `DELETE /v1/users/{user_id}` удаляет клиента из `clients`
7. Socks smoke проходит, chain до exit работает
8. `GET /v1/users/{user_id}/limit-policy` возвращает `mode=unlimited` (дефолт)
9. `PUT /v1/users/{user_id}/limit-policy` с `mode=limited` принимается без ошибок
10. `tc -s qdisc show` показывает htb qdisc на egress-интерфейсе (если `LIMITED_TC_ENABLED=true`)

Если все пункты выполнены, установка успешна.
