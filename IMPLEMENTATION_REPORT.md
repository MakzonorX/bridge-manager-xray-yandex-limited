# IMPLEMENTATION_REPORT.md

## Обзор

Реализована продуктовая фича **ограничения трафика на уровне пользователя** для аварийных/дорогих bridge-node (Yandex Cloud сценарий).

Два режима:
- **Unlimited** — без ограничений (поведение по умолчанию, backward-compatible).
- **Limited** — лимит трафика в байтах с двумя политиками после достижения:
  - `throttle` — замедление до 100 KB/s (102400 B/s по умолчанию) через реальный network shaping (tc + fwmark).
  - `block` — полная блокировка трафика через blackhole outbound Xray.

---

## Изменённые файлы

| Файл | Что изменено |
|---|---|
| `app/models.py` | Добавлена модель `UserLimitPolicy` (новая таблица `user_limit_policies`) |
| `app/settings.py` | Добавлены env-переменные для tc/throttle: `LIMITED_THROTTLE_RATE_BYTES_PER_SEC`, `LIMITED_TC_ENABLED`, `LIMITED_TC_EGRESS_IFACE`, `LIMITED_TC_MARK`, `LIMITED_TC_CLASS_ID`, `LIMIT_POLL_INTERVAL_SECONDS` |
| `app/main.py` | Добавлены endpoint'ы `GET/PUT /v1/users/{user_id}/limit-policy`, pydantic-модель `LimitPolicyRequest` с валидацией, `EnforcementLoop` в startup/shutdown, `limit_policy` в сериализацию пользователя |
| `app/xray_config.py` | Добавлены функции `_ensure_throttle_outbound`, `_ensure_blocked_outbound`, `apply_enforcement_routing` для динамической генерации routing rules enforcement |
| `app/enforcement.py` | **Новый файл.** Модуль enforcement: `check_and_enforce_limits`, `apply_current_enforcement`, `clear_enforcement`, `reapply_enforcement_routing`, `EnforcementLoop` |
| `scripts/setup_tc.sh` | **Новый файл.** Идемпотентный скрипт настройки tc (HTB qdisc + fw filter) для rate limiting по fwmark |
| `scripts/bootstrap_bridge.sh` | Добавлен `iproute2` в зависимости, вызов `setup_traffic_shaping()`, новые env-переменные в `write_app_env` |
| `scripts/smoke.sh` | Добавлены шаги проверки limit-policy API |
| `deploy/env.example` | Добавлены новые env-переменные |
| `tests/test_enforcement.py` | **Новый файл.** 30 тестов: валидация policy, state transitions, Xray config generation, smoke integration |
| `README.md` | Обновлён: описание фичи, новые API, env-переменные, troubleshooting |
| `IMPLEMENTATION_REPORT.md` | **Этот файл.** |

---

## Схема enforcement

### Архитектура

```
TrafficCollector (каждые 15с) → persists traffic totals → DB
                                                            ↓
EnforcementLoop (каждые 15с)  → check_and_enforce_limits() → reads UserLimitPolicy + UserTraffic
                                                            ↓
                              if total_bytes >= traffic_limit_bytes:
                                set enforcement_state = "throttled" | "blocked"
                                                            ↓
                              apply_current_enforcement() → mutate Xray config → restart Xray
```

### Xray config routing

При enforcement:
1. Для throttled пользователей создаётся routing rule: `user:email → to-exit-throttled` outbound.
2. Для blocked пользователей: `user:email → blocked` (blackhole) outbound.
3. Правила вставляются **после** API rule, но **до** дефолтного `inbound-from-users → to-exit`.
4. Outbound `to-exit-throttled` — копия `to-exit`, но с `streamSettings.sockopt.mark = <LIMITED_TC_MARK>`.

### Как работает tc

```
tc qdisc add dev <iface> root handle 1: htb default 99
tc class add dev <iface> parent 1: classid 1:99 htb rate 10gbit     # unlimited default class
tc class add dev <iface> parent 1: classid 1:10 htb rate 819kbit    # throttled class (102400 B/s)
tc filter add dev <iface> parent 1:0 protocol ip prio 1 handle 100 fw flowid 1:10
```

Xray outbound `to-exit-throttled` устанавливает `SO_MARK = 100` на сокет. Ядро Linux маркирует исходящие пакеты fwmark=100. tc filter с `fw` classifier перенаправляет их в class 1:10 (rate limited).

### Защита от дребезга

- Enforcement loop проверяет только пользователей с `enforcement_state = "none"` и `mode = "limited"`.
- Уже throttled/blocked пользователи **не** переобрабатываются.
- Xray config не перегенерируется, если enforcement rules не изменились (idempotent check).

---

## Воспроизведение сценария throttle

```bash
# 1. Создать пользователя
curl -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test-user","label":"Test"}'

# 2. Установить policy: лимит 10 GB, throttle после достижения
curl -X PUT http://127.0.0.1:8080/v1/users/test-user/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"limited","traffic_limit_bytes":10737418240,"post_limit_action":"throttle","throttle_rate_bytes_per_sec":102400}'

# 3. Когда трафик (uplink+downlink) достигнет 10 GB:
#    - enforcement_state переключится на "throttled"
#    - Xray config обновится: routing rule направит user:test-user в to-exit-throttled
#    - tc ограничит пакеты с fwmark=100 до 102400 B/s

# 4. Проверить состояние
curl http://127.0.0.1:8080/v1/users/test-user/limit-policy \
  -H "Authorization: Bearer $TOKEN"

# 5. Снять ограничение
curl -X PUT http://127.0.0.1:8080/v1/users/test-user/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"unlimited"}'
```

## Воспроизведение сценария block

```bash
curl -X PUT http://127.0.0.1:8080/v1/users/test-user/limit-policy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"limited","traffic_limit_bytes":5368709120,"post_limit_action":"block"}'

# После достижения 5 GB:
#   - enforcement_state = "blocked"
#   - Xray routing: user:test-user → blocked (blackhole)
#   - Пользователь не может передавать трафик
#   - UUID и ключ НЕ удаляются, пользователь НЕ удаляется из БД
```

---

## Ограничения и компромиссы

### 1. Точность лимита
Из-за polling-based архитектуры (TrafficCollector каждые 15с) точный hard-cut по байту невозможен. Возможен перелёт лимита на объём трафика за один интервал опроса + in-flight данные. Это документированное и ожидаемое ограничение.

### 2. Перезапуск Xray при изменении enforcement
Применение throttle/block требует обновления Xray config и перезапуска Xray. Это соответствует текущей operational model проекта. Все существующие соединения разрываются при рестарте.

### 3. tc shaping работает только для исходящего трафика
tc qdisc на egress interface ограничивает исходящие пакеты. Входящий трафик (download с точки зрения VPN пользователя) ограничивается, т.к. Xray проксирует трафик через exit-node, и ответные данные идут через тот же outbound сокет.

### 4. tc не персистентен между перезагрузками сервера
tc rules сбрасываются при reboot. Нужно вызывать `scripts/setup_tc.sh` при старте или через systemd unit. Bootstrap скрипт настраивает tc при первоначальном деплое.

### 5. Один rate limit на весь throttle lane
Все throttled пользователи делят одну tc class. Если нужен per-user rate limit — потребуется отдельный outbound + tc class на каждого пользователя, что значительно усложнит реализацию.

---

## Логирование

Все события логируются в формате, удобном для grep:

```
policy_changed user_id=alice old_mode=unlimited new_mode=limited ...
limit_reached user_id=alice total_bytes=10737418240 limit=10737418240 action=throttle
xray_reload_started throttled=['alice'] blocked=[]
xray_reload_succeeded
throttle_applied user_id=alice
enforcement_cleared user_id=alice old_state=throttled
```
