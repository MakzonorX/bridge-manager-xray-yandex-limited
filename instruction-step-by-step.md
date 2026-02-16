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

---

## 1) Данные, которые подготовить заранее

На своей стороне подготовьте 3 обязательных параметра:

1. `BRIDGE_DOMAIN`
- Пример: `test-bridge.example.com`
- Требование: A-запись этого домена уже указывает на публичный IP нового сервера.

2. `ACME_EMAIL`
- Email для выпуска TLS сертификата через Let's Encrypt/acme.sh.

3. `API_TOKEN`
- Длинный секрет для `Authorization: Bearer <token>`.

Опционально:

4. `API_PUBLIC`
- `false` (рекомендовано): API только на `127.0.0.1:8080`.
- `true`: API слушает `0.0.0.0:8080` и открывается в UFW.

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

Запустите так:

```bash
sudo BRIDGE_DOMAIN='<ВАШ_ДОМЕН>' \
ACME_EMAIL='<ВАШ_EMAIL>' \
API_TOKEN='<ВАШ_API_TOKEN>' \
API_PUBLIC=false \
./scripts/bootstrap_bridge.sh
```

Опционально можно добавить:

```bash
DISABLE_IPV6=true
```

Пример полного запуска:

```bash
sudo BRIDGE_DOMAIN='bridge.example.com' \
ACME_EMAIL='admin@example.com' \
API_TOKEN='super_secret_token_value' \
API_PUBLIC=false \
DISABLE_IPV6=true \
./scripts/bootstrap_bridge.sh
```

Что делает скрипт автоматически:

1. Устанавливает системные пакеты.
2. Включает синхронизацию времени.
3. Настраивает UFW (`22`, `80`, `443`, и `8080` если `API_PUBLIC=true`).
4. Ставит Xray `v26.2.6`.
5. Выпускает TLS сертификат через acme.sh standalone (`:80`).
6. Пишет Xray конфиг bridge.
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

`0/0` допустимо.

### 7.5 Удалить пользователя

```bash
curl -s -X DELETE http://127.0.0.1:8080/v1/users/demo-user-1 \
  -H "Authorization: Bearer $TOKEN" | jq .
```

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

---

## 14) Обновление на сервере

Если в репозитории появились изменения:

```bash
cd /opt/bridge-manager
git pull
sudo BRIDGE_DOMAIN='<ВАШ_ДОМЕН>' \
ACME_EMAIL='<ВАШ_EMAIL>' \
API_TOKEN='<ВАШ_API_TOKEN>' \
API_PUBLIC=false \
./scripts/bootstrap_bridge.sh
```

Скрипт переустановит/актуализирует состояние.

---

## 15) Минимальный чеклист "готово к работе"

1. `systemctl is-active xray` -> `active`
2. `systemctl is-active bridge-manager` -> `active`
3. `curl http://127.0.0.1:8080/health` -> `status=ok`
4. `POST /v1/users` выдаёт валидный `vless_uri`
5. Пользователь появляется в `clients` inbound `inbound-from-users`
6. `DELETE /v1/users/{user_id}` удаляет клиента из `clients`
7. Socks smoke проходит, chain до exit работает

Если все пункты выполнены, установка успешна.
