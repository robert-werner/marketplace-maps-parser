# Roadmap: Wildberries — обход HTTP 498

Состояние на 2026-11-08 (master `b10b99e`, pytest **521 passed**).
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

## Найденные дефекты текущего кода

1. **`--wb-api-only` мёртв** [код+изм.]: `_check_api_available` / `_iter_api_only` (`wb_browser.py:266`, `:671`) бьют в `www.wildberries.ru/api/v2/catalog/{nm}/reviews` — несуществующий эндпоинт на охраняемом хосте; он всегда отдаёт 498-челлендж → всегда fallback в браузер. Прошлый вывод «pre-check failed из-за SOCKS» — неверная атрибуция.
2. **Прокси-авторизация теряется в httpx** [код]: берётся только `proxy["server"]`, `username/password` отбрасываются.
3. **Синхронный `httpx.Client` внутри async** [код]: блокирует event loop (параллельные сессии).
4. **498 считается перманентной блокировкой и «сжигает» прокси** [код]: `_wb_error(498)` → `HTTP498Error(retryable=False)` → `mark_blocked`. Но 498 — челлендж, который браузер решает. При этом ждём ссылку на отзывы лишь 30 s (`wb_browser.py:473`), а измеренный путь «челлендж + рендер SPA» на residential = 21 s → на медленном прокси хороший IP сжигается.
5. **Успех челленджа определяется по селектору ссылки**, а не по собственному сигналу (cookie `x_wbaas_token` / `create-token` 200) [код]; блок слоя-2 («выключить VPN») не распознаётся вообще — он выглядит как timeout.
6. **`_WB_STEALTH_JS` включён по умолчанию** (`use_stealth=True`) и переписывает `hardwareConcurrency=16`, `deviceMemory`, `languages` поверх отпечатка Invisible Playwright — противоречит `browser_common.py` («No page-level stealth shim»), создаёт рассогласование отпечатка [код; влияние на детект — гипотеза].
7. **Токен не сохраняется**: `--save-cookies` есть только у Yandex; решённый WB-токен (14 дней) выбрасывается с каждой сессией [код].
8. **`incomplete_reason` ложный на API-пути**: ответ `/feedbacks/v2` полный, но `feedback_next_url` → None → «coverage unverified» [код].
9. **Безопасность**: `proxies.txt` (1000 строк с логинами/паролями) закоммичен в git (`6beabde`), в `.gitignore` его нет [изм.].

## Roadmap

### P0 — браузерless API как основной путь (закрывает 498)
- [ ] `wb_api.py`: `resolve_imt(nm)` — `card.json` с basket-хоста: таблица vol→basket + fallback-пробинг `basket-01…NN` параллельно (async, ~1 s), кэш по vol. Из `card.json` же — `imt_name` (title).
- [ ] `fetch_all_feedbacks(imt)` — `feedbacks1.wb.ru` → `feedbacks2.wb.ru` → шард из `feedback-bt…/host` при ошибке; фильтр по `nmId` (как `feedback_cards`); `total_count=feedbackCount`, `average_rating=valuation`; `incomplete_reason` только если `len(text-feedbacks) < feedbackCountWithText`.
- [ ] Сделать этот путь **первым по умолчанию** для WB; браузер — fallback (сетевая ошибка/формат). `--wb-api-only` = «без fallback». Удалить фейковый эндпоинт и pre-check.
- [ ] `httpx.AsyncClient`, прокси с auth (`scheme://user:pass@host:port`), retry/backoff на 429/5xx.
- [ ] Тесты на фикстурах реальных ответов (`card.json`, `feedbacks/v2`, сохранить в `tests/fixtures/wb/`); live-smoke: 2 товара, без прокси.
- Ожидаемый эффект: сбор WB без браузера и без прокси, секунды вместо минут [изм. на 2 товарах; масштаб не проверен].

### P1 — браузерный fallback перестаёт сжигать хорошие IP
- [ ] Челлендж-стадия: ждать `x_wbaas_token` (до 60 s), а не ссылку; 498 → `retryable` (не `mark_blocked`).
- [ ] Детект слоя-2: текст «выключить VPN» / отсутствие feedback-XHR после токена → `IPReputationError` (вот это — перманентно, сжигать прокси).
- [ ] `use_stealth` по умолчанию **off** (Invisible Playwright сам владеет отпечатком); A/B по проходу челленджа на одном прокси — до смены дефолта.
- [ ] Сохранять `x_wbaas_token` в cookie-пул / `--save-cookies` для WB; переиспользовать в httpx для `www`-страниц (TTL 14 дней).

### P2 — покрытие и эксплуатация
- [ ] Оценки без текста (1368 vs 811 у 150479920) в `/feedbacks` не приходят — задокументировать в выводе (`rating_only_count = feedbackCount − feedbackCountWithText`) [изм.].
- [ ] Проверить потолок ответа: внешний источник (actorforge, 07.2026) утверждает, что `/feedbacks/v1` отдаёт максимум последние 1000 отзывов без пагинации, а при отсутствии отзывов — `feedbacks: null`; у нас v2 отдал 811 (< 1000), т.е. потолок не проверен. Нужен товар с >1000 текстовых отзывов; при подтверждении — добирать остаток браузерным DOM-скроллом и честный `incomplete_reason` [гипотеза].
- [ ] Рейт-лимит `feedbacks1/2` при параллельном сборе (источник: 30 параллельных → 30×200) и TLS-отпечаток клиента (источники: часть HTTP-клиентов получают HTML антибота на `www`; при необходимости `curl_cffi` impersonate) [не измерено у нас].
- [ ] Серверный прогон (otzovik, `84.38.2.45:8021`): API-путь без `MARKETPLACE_WB_PROXY` — подтвердить с серверного IP.
- [ ] `proxies.txt` → убрать из индекса (`git rm --cached`), в `.gitignore`, ротировать креды пула.

### Закрыто / не делать
- ✗ Свипы прокси-пулов по curl-498 для WB — 498 получает любой клиент без токена, такой свип ничего не классифицирует.
- ✗ Stealth/UA-подмена как средство против 498 — челлендж проходит и без неё; блок слоя-2 — по IP, не по отпечатку.
- ✗ `www.wildberries.ru/__internal/u-card/cards/v4/detail` без токена — 403 [изм.]; не нужен при `card.json`.

## Воспроизведение
Пробы: `~/.hermes/cache/scratch/wb_probe/probe5.py` (браузер через прокси, docker `marketplace-maps-parser:fixed`, `--env-file proxy.env`), curl-проверки — в этом документе выше. Scratch чистится через 24 ч — фикстуры для P0 сохранять в репозиторий.
