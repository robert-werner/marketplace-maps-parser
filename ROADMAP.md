# Roadmap: Wildberries — обход HTTP 498

Состояние на 2026-11-08: P0–P1 выполнены, P2 частично (pytest **534 passed**).
Метки: **[изм.]** — измерено в этой сессии (n указано), **[код]** — следует из чтения кода, **[гипотеза]** — не измерено.

## Что на самом деле такое 498 (измерено)

| Факт | Доказательство |
|---|---|
| 498 на `www.wildberries.ru` — это **JS-челлендж WBAAS**, его получает любой клиент без cookie `x_wbaas_token`, с любого IP. Это не вердикт по IP. | `server: wbaas`, `x-wbaas-token: get`, тело — `/__wbaas/challenges/antibot/…/browser-check.js` + site-key; curl с OVH-IP и с residential-IP — одинаковый 498 [изм., n=3] |
| Браузер (Invisible Playwright) **проходит челлендж** и с datacenter-IP (OVH, ~10 s), и с residential (AKADO Москва, ~21 s до ссылки на отзывы); выдаётся `x_wbaas_token`, срок жизни **cookie** 336 ч (14 дней; серверная валидность токена не измерена — источники называют от минут до 14 дней) | `create-token` 498→200, cookie в контексте [изм., n=4] |
| Второй слой — **репутация/гео IP на уровне SPA**: с OVH после токена страница показывает «Возможно, нужно выключить VPN», отзывы не грузятся; с RU-residential — 27 карточек | probe3/probe5 [изм., n=1 на каждый IP] |
| Токен **переиспользуется вне браузера**: httpx + `x_wbaas_token` + тот же UA → 200 на `detail.aspx` и `/feedbacks`; без cookie → 498. Сработало и после смены egress-IP шлюза (92.243.182.105 → 46.242.8.254) | probe1/probe5 [изм., n=2; привязка к IP не исключена для других подсетей] |
| **Данные отзывов лежат на неохраняемых хостах.** SPA берёт: `basket-NN.wbbasket.ru/volV/partP/{nm}/info/ru/card.json` → `imt_id`; `feedback-bt.wildberries.ru/feedback/api/v2/host?imt=` → шард; `feedback-view-02.wb.ru/feedbacks/v2/{imt}` | лог трафика probe5 [изм.] |
| Эти хосты отвечают **200 без токена, без браузера, без прокси — даже с OVH datacenter-IP**. `feedbacks1.wb.ru` / `feedbacks2.wb.ru` / `feedback-view-02.wb.ru` `/feedbacks/v1|v2/{imt}` отдают одинаковый ответ | curl direct и через прокси [изм., n=2 товара] |
| Один GET возвращает **все отзывы с текстом** по imt (все варианты): товар 150479920 — 811 записей за 0,6 s (231 KB), из них 454 по этому nm; `feedbackCount`=1368 включает оценки без текста, их в ответе нет. Параметры `skip/take/page` игнорируются | [изм., n=1 крупный товар] |

**Вывод:** 498 обходится не stealth'ом и не прокси, а **уходом с `www.wildberries.ru`**: отзывы берутся из `card.json` + `feedbacks{1,2}.wb.ru`. Браузер и residential-прокси остаются только запасным путём.

## Статус дефектов (2026-11-08, после P0–P2)

| # | Дефект | Статус |
|---|---|---|
| 1 | `--wb-api-only` бил в несуществующий `www…/api/v2/catalog/{nm}/reviews` | ✅ удалён; новый `wb_public_api.py` |
| 2 | прокси-auth терялся в httpx | ✅ `proxy_url()` сохраняет логин/пароль (тест) |
| 3 | синхронный `httpx.Client` в async | ✅ `httpx.AsyncClient` |
| 4 | 498 сжигал прокси, ожидание 30 s | ✅ ждём cookie до 60 s; нет токена → 498 (сжечь); токен есть, страница не отрисовалась → session-restart без сжигания |
| 5 | слой-2 («выключить VPN») не распознавался | ✅ `IPReputationError` (unit-тест; live не воспроизвелось — см. ниже) |
| 6 | stealth-shim по умолчанию | ✅ `use_stealth=False` по умолчанию; A/B со stealth-on не делался — основание: 5 проб + 2 live-прогона без shim прошли челлендж |
| 7 | токен не сохранялся | ✅ `--save-cookies` для WB пишет jar (`x_wbaas_token`, `_wbauid`) |
| 8 | ложный `incomplete_reason` на API-пути | ✅ только при упоре в окно ~1000 |
| 9 | `proxies.txt` с кредами в git | ✅ снят с индекса. **Поправка:** он уже был в `.gitignore` (стр. 59) — прошлый пункт «в `.gitignore` его нет» был неверным (вывод сделан по обрезанному `head`); файл закоммичен до правила, в истории (`6beabde`) остаётся |

## Измерено после внедрения

| Проверка | Результат |
|---|---|
| CLI `--wb-api-only`, без прокси, с OVH-IP, macOS без docker | 831948063: 29 отзывов, 0,9 s; 150479920: 454, 1,1 s; 13738266: 677, 1,1 s, `status: partial` (окно), exit 3 |
| Окно API (imt 10283579, 142 192 текстовых) | v1 — ровно 1000, v2 — 1015, самые новые; v2 для 502167605 — 1012 [изм., n=2] |
| `feedbacks: null` у пустого imt | подтверждено (`/feedbacks/v2/1`), `valuation: ""` |
| 30 параллельных GET на `feedbacks1` и на `basket-38` | 30×200 и 30×200 (один залп) |
| Серверный IP otzovik `84.38.2.45` | `feedbacks1` 200, `card.json` 200 (curl) |
| Браузерный fallback (`--no-browser-api`), docker, residential-прокси | 27 отзывов, 25,7 s, cookies сохранены |
| То же без прокси (OVH) | 27 отзывов, 18,9 s — «выключить VPN» на этот раз **не** воспроизвелось (раньше 1 из 2 проб) → слой-2 на OVH нестабилен |
| pytest | **534 passed** (было 521: −2 старых api-only, +6 public_api, +6 public_path, +3 challenge) |

## Roadmap

### P0 — браузерless API как основной путь ✅
- [x] `wb_public_api.resolve_card`: параллельный пробинг `basket-01…64`, кэш vol→host (таблица vol→basket отвергнута: её значения не измерены).
- [x] `fetch_feedbacks`: `feedbacks1` → `feedbacks2` → шард `feedback-bt…/host`; retry/backoff 429/5xx/transport.
- [x] Путь первый по умолчанию; `--wb-api-only` — без fallback; `--no-browser-api` выключает его.
- [x] Фикстуры реальных ответов `tests/fixtures/wb/`, live-smoke на 3 товарах.

### P1 — браузерный fallback не сжигает хорошие IP ✅
- [x] Челлендж по cookie `x_wbaas_token` (60 s); 498 только без токена.
- [x] `IPReputationError` по «выключить VPN».
- [x] `use_stealth` off по умолчанию.
- [x] `--save-cookies` для WB.
- [ ] ⏳ Повторное использование сохранённого jar через `--cookies` (пропуск челленджа ~10–20 s) — не проверено live.

### P2 — покрытие и эксплуатация
- [x] `diagnostics.rating_only_count`, `incomplete_reason` в выводе WB.
- [x] Потолок API подтверждён (~1000 новейших) → `status: partial`.
- [x] Параллельный залп 30×200; TLS-отпечаток httpx не мешает (200) — `curl_cffi` не нужен.
- [x] Серверный IP otzovik — 200 на обоих хостах (curl).
- [x] `proxies.txt` снят с индекса.
- [ ] ⏳ Отзывы старше окна ~1000: проверить, отдаёт ли сайт (DOM-скролл) больше, чем API. Если да — fallback в браузер при `window_limited`. Сейчас не делается: SPA ходит в тот же `/feedbacks/v2` [гипотеза].
- [ ] ⏳ Рейт-лимит при длительном массовом сборе (сотни товаров подряд) — не измерен.
- [ ] ⏳ Обёртка otzovik: прогон `/parse` для WB на сервере после выкатки образа; новых env не нужно (API-путь включён по умолчанию).
- [ ] 👤 Ротация кредов: пул из `proxies.txt` (в истории git) и прокси `pool.proxy.market` (передан в чат) — действие пользователя.

### Закрыто / не делать
- ✗ Свипы прокси по curl-498 для WB — 498 получает любой клиент без токена.
- ✗ Stealth/UA-подмена против 498 — челлендж проходит и без неё.
- ✗ Таблица vol→basket — заменена пробингом.
- ✗ `curl_cffi`/impersonate — httpx получает 200 на публичных хостах.
- ✗ `www.wildberries.ru/__internal/u-card/cards/v4/detail` без токена — 403; не нужен.

## Воспроизведение
Браузерная проба: `~/.hermes/skills/software-development/marketplace-maps-parser/scripts/wb_probe.py` (docker-команда в docstring). API: `uv run marketplace-maps-parser --url https://www.wildberries.ru/catalog/<nm>/detail.aspx --wb-api-only --output x.json`. Фикстуры — `tests/fixtures/wb/`.
