# marketplace-maps-parser

Асинхронный сборщик отзывов Ozon, Wildberries, Яндекс.Маркета, Яндекс.Карт,
2GIS и профилей Avito.
Все отзывы получают браузерные транспорты на **Invisible Playwright**: DOM,
навигация к JSON API или `fetch()` из JavaScript-контекста страницы.

## Установка

Python 3.14+, `uv` и браузерный движок, соответствующий установленной версии
Invisible Playwright:

```bash
uv sync --locked --extra dev
uv run python -m marketplace_maps_parser --help
```

При несовпадении движка Invisible Playwright и версии пакета используйте:

```bash
uv run python -m invisible_core doctor
```

Браузер запускается с видимым окном; среде нужен дисплей. Cookie-файлы и
учётные данные прокси не следует добавлять в Git. `.env.example` — исторический
шаблон: текущий CLI не загружает `.env`, передавайте параметры явно.

## Браузерный транспорт

Для всех источников используется Invisible Playwright. Для Ozon единственное
значение `--transport` — `playwright`; он запускает `BrowserJsonTransport`.

Invisible Playwright формирует fingerprint внутри модифицированного движка.
Проект не накладывает поверх него JS-подмены `navigator`, `plugins`,
`languages`, `window.chrome` или User-Agent: такие shim-слои могут создать
противоречия между HTTP-заголовками, DOM и внутренним fingerprint.

Отдельных HTTP-клиентов и транспорта публичной страницы Ozon нет.

У Ozon доступны три **стратегии получения JSON внутри браузера**:

- `--fetch-strategy auto` (по умолчанию): дождаться содержимого отзывов,
  затем вызывать внутренний `/api/entrypoint-api.bx/page/json/v2` через
  `fetch()` без перезагрузок. При сбое вкладка переключается на API-навигацию;
- `--fetch-strategy navigation`: браузер переходит на API URL;
- `--fetch-strategy fetch`: запрос выполняется через `page.evaluate(fetch(...))`
  в текущей сессии, с её cookies, без автоматического переключения.

Следующие страницы берутся из `nextPage`, без угадывания токенов/номеров.
Firefox JSON Viewer читается через исходный JSON, а не текст дерева интерфейса.
Состояние fallback и pacing изолировано между вкладками.

`--strategy` — не другой транспорт, а способ обхода отзывов:

- `auto` — API-пагинация с DOM-scroll как резервом/дополнением;
- `pagination` — API-пагинация без scroll;
- `scroll` — DOM-scroll внутри того же Invisible Playwright-транспорта.

Яндекс.Маркет, Яндекс.Карты, 2GIS, Wildberries и Avito используют собственные
адаптеры, но тот же браузерный runtime. Для Яндекс.Маркета сначала разбирается
структурированный `reviewList/ugcItems` state, затем следующие страницы
запрашиваются через `fetch` в той же вкладке. Для Avito профильный `/ratings`
API используется после открытия кнопки «Ещё отзывы»; курсор принимается только
на том же endpoint. Для Wildberries и 2GIS API-путь включается только после
перехвата запроса самой страницы — не используются угаданные shard/ID/токены.
Яндекс.Карты продолжают использовать подписанный `fetchReviews` API с UI
fallback. Все эти запросы выполняются через `page.evaluate`, а не отдельный
HTTP-клиент.

`--no-browser-api` отключает быстрые API/state-пути WB, Яндекс.Маркета,
Яндекс.Карт, 2GIS и Avito для сравнения или диагностики; браузерный DOM/SSR
fallback остаётся.

## Использование

Источник определяется по URL; `--marketplace` нужен явно только для списка
товаров. Все примеры используют браузерный режим.

```bash
# Ozon: один товар
uv run marketplace-maps-parser \
  --url "https://www.ozon.ru/product/<slug>-<id>/" \
  --cookies ozon_cookies.json --output data/ozon.json

# Ozon: JSON API через fetch внутри Invisible Playwright
uv run marketplace-maps-parser \
  --url "https://www.ozon.ru/product/<slug>-<id>/" \
  --strategy pagination --fetch-strategy fetch \
  --cookies ozon_cookies.json --output data/ozon.json

# Другие источники
uv run marketplace-maps-parser --url "https://market.yandex.ru/card/<slug>/<id>"
uv run marketplace-maps-parser --url "https://yandex.ru/maps/org/<slug>/<id>/"
uv run marketplace-maps-parser --url "https://2gis.ru/moscow/firm/<id>"
uv run marketplace-maps-parser --url "https://www.wildberries.ru/catalog/<id>/detail.aspx"

# Возобновление и отдельный формат JSONL
uv run marketplace-maps-parser \
  --url "https://www.ozon.ru/product/<slug>-<id>/" \
  --format jsonl --output data/reviews.jsonl --resume
```

### Несколько товаров / независимых сессий

```bash
# Один URL на строку; пустые строки и комментарии # допускаются
uv run marketplace-maps-parser \
  --marketplace ozon --products-file products.txt --products-sessions 3 \
  --proxy-list proxies.txt --cookies ozon_cookies.json \
  --output data/products.json

# Диапазон API-страниц одного Ozon-товара, по прокси на процесс
uv run marketplace-maps-parser \
  --url "https://www.ozon.ru/product/<slug>-<id>/" \
  --parallel-sessions 3 --max-pages 30 \
  --proxy-list proxies.txt --cookies ozon_cookies.json \
  --output data/ranges.json
```

Дочерние процессы используют только Invisible Playwright, наследуют формат,
`--fetch-strategy`, cookies, задержки и настройки checkpoint. Для каждого
создаётся отдельная debug-директория. Объединение учитывает ошибки детей,
дедуплицирует записи и сохраняет промежуточные файлы при неполном результате.
Диапазон страниц не гарантирует полноту списка: пагинация источника может
использовать session/page-key и пересекающиеся окна.

### Cookies и прокси

- `--cookies FILE`: JSON-список Playwright/DevTools или Netscape cookie file;
- `--proxy URL`: один прокси на браузерную сессию;
- `--proxy-list FILE`: при параллельных браузерных потоках Ozon выдаёт
  отдельный прокси каждой новой сессии; при ошибке `--proxy-attempts`
  может повторить запуск со следующим прокси. Ротации внутри уже
  запущенной сессии нет;
- `--free-proxy`: служебное получение бесплатных прокси; они могут быть
  медленными и нестабильными;
- Яндекс.Маркет сохраняет cookies и умеет перезапускать сессию при блокировке.

## Формат и контрольные сохранения

По умолчанию `--format json`, имя файла — `reviews.json`:

```json
{
  "reviews": [{
    "source_url": "https://…", "platform": "ozon",
    "product_title": "Название", "text": "Текст отзыва", "rating": 5,
    "review_date": "2026-10-10", "photos": 0, "video_len": null,
    "text_len": 12, "raw": {"reviewId": "123"}
  }],
  "diagnostics": {
    "status": "complete", "error": null, "collected": 1,
    "total_records": 1, "completeness_verified": false
  }
}
```

`photos` — количество фото, `video_len` — известная длительность видео в
секундах или `null`. Достоинства и недостатки включаются в `text`. В JSONL
сохраняются отдельные поля `pros`, `cons`, `author`, `review_id`, `raw`.

- `complete`: выбранный обход завершён без обнаруженной неполноты;
- `partial`: достигнут лимит, не достигнут известный total, произошла отмена
  или ошибка после получения части данных;
- `failed`: ошибка без сохранённых отзывов.

**`complete` не доказывает полноту площадки**, когда неизвестен её total:
проверяйте `completeness_verified`, `expected_count` и диагностические поля.
Коды CLI: `0` — complete, `3` — partial, `2` — failed, `130` — Ctrl+C.

Во время JSON-сбора данные дописываются в `<output>.checkpoint.jsonl`, а
метаданные — в `<output>.status.json`. Журнал синхронизируется каждые 100 новых
отзывов или 5 секунд; параметры — `--checkpoint-interval` и
`--checkpoint-seconds`. После штатной остановки, ошибки или отмены JSON
формируется потоково и заменяется атомарно. При жёстком завершении процесса
`--resume` восстанавливает сохранённый журнал. Последний неполный фрагмент
записи может быть отброшен; повреждение внутри файла не игнорируется.

JSONL дописывается непосредственно, с теми же checkpoint и status-sidecar.
В памяти остаются ключи дедупликации, а не все payload отзывов. Чтение старого
JSON для resume и объединение JSON-частей пока загружают соответствующий
документ в память.

## Производительность и ограничения

- Браузерная сессия используется повторно в пределах API-потока Ozon.
- Готовность отзывов проверяется вместо фиксированной паузы: API-запрос
  не отправляется из промежуточного документа во время редиректа.
- API-пагинация не создаёт полные `Review` повторно только для подсчёта.
- `--page-delay-seconds` действует внутри каждого API-потока, в том числе
  параллельного; нет задержки после последней страницы.
- DOM-карточки Ozon читаются одним пакетным вызовом вместо множества
  последовательных обращений к атрибутам.
- Запись результатов буферизована; checkpoint не переписывает весь JSON.
- `--parallel-streams` запускает независимые сортировочные потоки Ozon.
- По умолчанию Ozon сортировочные потоки запускаются параллельно; для
  последовательного режима используется `--serial-streams`.
- `--maps-api-concurrency` ограничивает число браузерных API-потоков Карт.
- HTML/JSON-дампы Ozon выключены по умолчанию; `--debug-dumps` включает их.
  `--debug-dir` задаёт каталог, но сам не включает дампы.
  `--screenshots` включает дампы и скриншоты. Дампы могут содержать данные
  аккаунта, их нельзя публиковать.
- Очередь параллельных потоков ограничена; достижение лимита закрывает
  вложенные итераторы и браузеры до возврата из сборщика, не через GC.
- Ненужные ресурсы Ozon блокируются по умолчанию; **`--no-block-assets`
  отключает блокировку**, а не включает её.
- В диагностиках сохраняется `collection_path`: например `api`,
  `browser_state_fetch`, `ssr+api`, `ssr`, `dom` или `dom_fallback`.
- Для Avito поддерживается ссылка профиля/бренда
  `/brands/i<id>/all`; это отзывы профиля, а не отзывы отдельных объявлений.
- Backoff, humanize и защитные задержки не обнуляются автоматически:
  увеличение параллелизма может повысить число блокировок.

Wildberries сначала пробует JSON ответ, перехваченный у текущей страницы,
затем DOM, если API недоступен. Его счётчик оценок не равен числу текстовых
отзывов; API-окно без доказательства полноты отмечается как partial.
Этот fast path пока проверен только на синтетических payload: live-проверка
10 октября 2026 на товаре `150479920` получила HTTP 498; ускорение WB
на реальной площадке пока не подтверждено.
2GIS читает SSR-окно за один переход, а при нехватке отзывов пробует
перехваченный endpoint текущего филиала. Пустой API не удаляет SSR-отзывы
и не считается доказательством полноты. `hasMore`/total остаются проверками.
Реальная скорость/полнота зависят от IP, cookies, объёма и состояния площадки;
unit-тесты не доказывают доступность сайтов.

### Live-бенчмарки остальных источников

```bash
uv run python scripts/benchmark_sites.py \
  --sites yandex avito yandex_maps 2gis wildberries \
  --modes fast --max-reviews 100 --max-pages 10

# Сравнение с DOM (те же лимиты; последовательные запуски)
uv run python scripts/benchmark_sites.py \
  --sites yandex --modes dom fast --max-reviews 30 --max-pages 3
```

Скрипт использует пять URL-примеров из задачи. `--proxy-list` и
`--proxy-index` выбирают один прокси, не выводя его credentials;
по умолчанию используется прямое соединение. Cookies Яндекс.Маркета и Карт
читаются из `--cookies-dir`, если файлы существуют. Cookies Ozon не передаются
другим площадкам. Результаты и сводка сохраняются в `data/sites_benchmark_*`.
Есть общий таймаут каждого варианта; отмена сохраняет частичный результат.
Это smoke-бенчмарк, не статистическая гарантия скорости или отсутствия блокировок.

### Воспроизводимый live-бенчмарк Ozon

```bash
uv run python scripts/benchmark_ozon.py \
  --url "https://www.ozon.ru/product/<slug>-<id>/" \
  --cookies cookie.json --proxy-list proxies.txt --proxy-index 51 \
  --strategies navigation auto --max-reviews 150 --max-pages 5
```

`--proxy-index` — номер строки среди валидных прокси (с 1); адреса и пароли
не выводятся. В `data/ozon_benchmark_<timestamp>/benchmark.json` сохраняются
время до первого отзыва, полное время, длительности успешных API-запросов,
число отзывов и достижение цели. Сравниваются одинаковые URL, cookies,
proxy, seed и лимит. Запуски последовательные: состояние сети/защиты может
измениться между ними. Если один вариант заблокирован, это не измерение
ускорения «в N раз». `partial` при заданном лимите — ожидаемый статус.

## Архитектура и проверки

```text
src/domain/                          модели Review / ProductRef
src/infrastructure/marketplaces/    нормализация пяти источников
src/infrastructure/transports/     браузерное получение отзывов
src/marketplace_maps_parser/
  collectors.py                    связывание адаптеров и транспорта
  transport_factory.py             lazy-конструирование транспорта
  runner.py                        lifecycle / статус / checkpoint
  output.py                        журнал и атомарная запись
  concurrency.py                   ограниченное объединение потоков
  parallel_sessions.py             дочерние процессы
  merging.py                       объединение результатов
```

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

CI запускает эти проверки без live-сбора и cookies. Лицензия: Proprietary.
