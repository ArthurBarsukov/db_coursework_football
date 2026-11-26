# Football Scouting — поліглотна система скаутингу

Програмна частина курсової роботи з дисципліни **«Бази даних та інформаційні системи»**
(КПІ ім. Ігоря Сікорського, ФПМ, група КМ-42).

Тема: *«Проєктування та розробка розподіленої високонавантаженої системи збору, збереження,
повнотекстового пошуку та моніторингу інформації»* на даних футбольного скаутингу
(відкритий датасет [Football Data from Transfermarkt](https://www.kaggle.com/datasets), Kaggle).

Поточний етап: **MongoDB як основне сховище даних** (етапи 10–11 курсової). Neo4j, Redis,
Elasticsearch і Kafka підключені до ядра через порти й поки що є заглушками.

## Зміст

- [Архітектурний підхід](#архітектурний-підхід)
- [Структура репозиторію](#структура-репозиторію)
- [Дані та модель MongoDB](#дані-та-модель-mongodb)
- [Індекси](#індекси)
- [Предметна логіка](#предметна-логіка)
- [Компоненти](#компоненти)
- [Порти та адаптери](#порти-та-адаптери)
- [DTO](#dto)
- [Запити до MongoDB](#запити-до-mongodb)
- [Запуск](#запуск)
- [Результати замірів](#результати-замірів)
- [Поточний стан і план](#поточний-стан-і-план)

---

## Архітектурний підхід

| Підхід | Як застосований |
|---|---|
| **Гексагональна архітектура** (Ports & Adapters) | Ядро (`application`) залежить лише від інтерфейсів-портів. Конкретні бази даних сховані в адаптерах. |
| **Шарувата архітектура** | Контролер → Фасад → Сервіс → Репозиторій (порт) → БД. Спілкування тільки зверху вниз. |
| **Facade** | `PlayerFacade` збирає профіль, виступи, клуб, турніри й тактичні аналоги в одне досьє гравця. |
| **Repository** | Кожен сервіс працює з вузьким репозиторієм-портом. |
| **Interface Segregation** | П'ять окремих портів для MongoDB; порти для графа, кешу, пошуку й черги не зачіпаються. |
| **CQRS** | Окремі контролери для читання (`PlayerQueryController`) і запису (`AppearanceCommandController`). |
| **DTO** | `@dataclass`-класи для передачі даних між шарами (`dto.py`). |
| **Graceful degradation** | Якщо граф недоступний (заглушка Neo4j), досьє повертається з попередженням `tactical_analogs_unavailable`. |
| **Polyglot Persistence** | MongoDB, Neo4j, Elasticsearch, Redis, Kafka: кожна технологія під свій профіль навантаження. |

Щоб замінити технологію (наприклад, MongoDB на PostgreSQL), достатньо змінити адаптер, не торкаючись
бізнес-логіки.

## Структура репозиторію

```
.
├── docker-compose.yml                   # replica set MongoDB rs0 (3 вузли) та ініціалізація
├── .env.example                         # шаблон змінних середовища (копіюється в .env)
├── requirements.txt
├── main.py                              # точка збирання: DI, наскрізна перевірка
├── command_service/
│   ├── config.py                        # клієнт MongoDB, колекції з write/read concern
│   ├── domain.py                        # формула оцінки виступу, агрегати гравця
│   ├── dto.py                           # DTO
│   ├── ports/
│   │   └── output.py                    # вихідні порти (Protocol)
│   ├── application/
│   │   └── services.py                  # сервіси та PlayerFacade
│   └── adapters/
│       ├── inbound/
│       │   └── controllers.py           # вхідні адаптери (контролери)
│       └── outbound/
│           ├── mongo_adapter.py         # MongoDB (реалізація)
│           ├── neo4j_adapter.py         # Neo4j (заглушка)
│           ├── redis_adapter.py         # Redis (заглушка)
│           ├── elastic_adapter.py       # Elasticsearch (заглушка)
│           └── kafka_adapter.py         # Apache Kafka (заглушка)
└── scripts/
    ├── setup_indexes.py                 # створення індексів (--stage pre | post)
    ├── etl_load_mongo.py                # завантаження датасету в MongoDB
    └── demo_mongo_queries.py            # демонстрація, explain, заміри (пише demo_results.txt)
```

## Дані та модель MongoDB

База: `football_scouting`. Датасет після очищення в ETL:

| Показник | Значення |
|---|---|
| Гравців | 50 149 |
| Клубів | 796 |
| Змагань | 65 |
| Виступів (appearances) | ≈ 1,89 млн |
| Гравців із виступами | 58,9 % (у середньому ≈ 64 виступи на такого гравця) |

| Колекція | Призначення | Особливості |
|---|---|---|
| `players` | Профіль гравця + **вкладений масив `appearances`** | Масив відсортований за датою (спадання); агрегати `total_matches`, `total_goals`, `avg_performance_score` зберігаються в документі |
| `clubs` | Довідник клубів | Унікальний `club_id` |
| `competitions` | Довідник змагань; `matches_count` | Унікальний `competition_id`; назви змагань — слаги (`bundesliga`), для відображення додаються `competition_id` і країна |
| `search_queries` | Телеметрія запитів | Наповнюється сервісами під час роботи; після ETL порожня |

Виступи вбудовано в документ гравця, бо досьє читається одним зверненням до БД (`$slice` бере
останні N виступів), а запис нового виступу атомарно змінює і масив, і лічильники.

Налаштування кластера: replica set `rs0` з трьох вузлів (порти `27017`, `27018`, `27019`).

| Операція | Write concern | Read concern / вибір вузла |
|---|---|---|
| Критичні (профілі, виступи) | `w="majority"`, `j=true` | читання з primary: `majority` |
| Звичайні читання | — | `secondaryPreferred` + `local` |
| Телеметрія | `w=1` | — |

## Індекси

Створюються скриптом `scripts/setup_indexes.py`.

| Колекція | Індекс | Призначення |
|---|---|---|
| `players` | `idx_players_player_id_unique` — `{player_id}` unique | Точковий пошук досьє, запис виступу |
| `clubs` | `idx_clubs_club_id_unique` — `{club_id}` unique | Довідник клубів |
| `competitions` | `idx_competitions_competition_id_unique` — `{competition_id}` unique | Довідник змагань |
| `players` | `idx_players_pos_subpos_value` — `{position, sub_position, market_value_eur: -1}` | Параметрична фільтрація за правилом ESR (equality → sort → range) |
| `players` | `idx_players_current_club_id` — `{current_club_id}` | Склад клубу |
| `players` | `idx_players_appearances_elem_match` — `{appearances.competition_id, appearances.minutes_played, appearances.goals}` (multikey) | Фільтрація за вкладеними виступами через `$elemMatch` |
| `search_queries` | `idx_search_queries_timestamp_desc` — `{timestamp: -1}` | Останні запити телеметрії |

Унікальні індекси створюються **до** завантаження даних (`--stage pre`), а індекси, що прискорюють
читання, — **після** нього (`--stage post`), щоб не уповільнювати ETL.

Нога (`foot`) навмисно не входить до складеного індексу: її фільтр застосовується на етапі `FETCH`.

## Предметна логіка

`command_service/domain.py` — чиста бізнес-логіка без залежності від БД.

Оцінка виступу (`performance_score`):

```
score = 5.0 + 1.5·голи + 1.0·асисти + 0.5·(хвилини / 90) − 0.5·жовті − 2.0·червоні
```

Результат обмежується діапазоном 0–10 і округлюється до 2 знаків. Якщо гравець зіграв **менше 10
хвилин**, рейтинг не обчислюється (`None`), і такий виступ не враховується в середньому
(`avg_performance_score`). Допустима тривалість виступу: 0–130 хвилин.

## Компоненти

### Контролери (`adapters/inbound/controllers.py`)

| Клас | Метод | Що робить |
|---|---|---|
| `PlayerQueryController` | `get_player(player_id, appearances_limit, consistent_read)` | Досьє гравця; `consistent_read=True` читає з primary |
| `PlayerQueryController` | `filter_players(criteria)` | Багатокритеріальна фільтрація |
| `PlayerQueryController` | `get_club_roster(club_id, limit)` | Склад клубу |
| `PlayerQueryController` | `get_defender_cards_stats()` | Картки захисників за амплуа |
| `PlayerQueryController` | `get_top_competitions(limit, min_matches)` | Топ турнірів за голами за матч |
| `PlayerQueryController` | `get_recent_queries(limit)` | Останні записи телеметрії |
| `PlayerQueryController` | `get_telemetry_summary(window_minutes)` | Зведення телеметрії за вікно часу |
| `AppearanceCommandController` | `record_match_appearance(request_data)` | Валідація та атомарний запис виступу |

Відповіді мають вигляд `{"status": "success" | "error" | ..., "data": ...}`.

### Сервіси та фасад (`application/services.py`)

| Клас | Призначення |
|---|---|
| `PlayerProfileService` | Профіль гравця та фільтрація |
| `AppearanceService` | Виступи гравця; атомарний запис |
| `ReferenceService` | Довідники: клуби, склад клубу, турніри |
| `AnalyticsService` | Агрегації (картки захисників, топ турнірів) |
| `TelemetryService` | Журнал і зведення телеметрії |
| `ScoutingService` | Тактичні аналоги (граф, поки заглушка) |
| `PlayerFacade` | Збирає `PlayerDossierDTO` з усіх сервісів |

Телеметрію запитів вмикає параметр `enable_telemetry`: запис іде в `search_queries`.

## Порти та адаптери

| Порт (`ports/output.py`) | Адаптер | Технологія | Стан |
|---|---|---|---|
| `PlayerProfileRepositoryPort` | `MongoAdapter` | MongoDB | реалізовано |
| `AppearanceRepositoryPort` | `MongoAdapter` | MongoDB | реалізовано |
| `ReferenceRepositoryPort` | `MongoAdapter` | MongoDB | реалізовано |
| `AnalyticsRepositoryPort` | `MongoAdapter` | MongoDB | реалізовано |
| `TelemetryRepositoryPort` | `MongoAdapter` | MongoDB | реалізовано |
| `GraphRepositoryPort` | `Neo4jAdapter` | Neo4j | заглушка |
| `CacheStorePort` | `RedisAdapter` | Redis | заглушка |
| `SearchIndexPort` | `ElasticsearchAdapter` | Elasticsearch | заглушка |
| `EventPublisherPort` | `KafkaPublisherAdapter` | Kafka | заглушка |

Розділення портів за принципом ISP дозволяє `MongoAdapter` явно реалізувати п'ять вузьких
контрактів.

## DTO

| Клас | Призначення |
|---|---|
| `PlayerDTO` | Профіль гравця та агрегати |
| `AppearanceDTO` | Один виступ: хвилини, голи, асисти, картки, `performance_score` |
| `PlayerWithAppearancesDTO` | `PlayerDTO` + список `AppearanceDTO` |
| `PlayerDossierDTO` | Досьє: гравець, виступи, клуб, турніри, аналоги, `warnings` |
| `PlayerFilterCriteria` | Критерії фільтрації, сортування та ліміт |
| `ClubDTO`, `CompetitionDTO` | Клуб і змагання |
| `CompetitionStatsDTO`, `DefenderCardsStatsDTO` | Результати агрегацій |
| `SearchQueryDTO`, `TelemetrySummaryDTO` | Запис і зведення телеметрії |
| `SaveResult` | Результат запису: `SAVED`, `DUPLICATE`, `PLAYER_NOT_FOUND` |

## Запити до MongoDB

Категорії відповідно до табл. 1.2 курсової роботи. MongoDB обслуговує категорії 2, 3, 6 і 7
та атомарний запис; категорія 1 (текстовий пошук) віддана Elasticsearch, категорії 4–5 (граф) — Neo4j.

| Кат. | Група | Запит | Підхід |
|---|---|---|---|
| 2 | Фільтрація | Гравці за позицією, амплуа та діапазоном вартості | складений індекс за ESR, сортування без `SORT` у плані |
| 2 | Фільтрація | Гравці з виступами за умовами (турнір, хвилини, голи) | `$elemMatch` + multikey-індекс |
| 3 | Читання | Досьє гравця | точковий пошук за `player_id`, `$slice` для масиву виступів |
| 3 | Читання | Склад клубу | індекс `current_club_id`, сортування за вартістю |
| 6 | Аналітика | Картки захисників за амплуа | агрегація `$unwind` + `$group` |
| 6 | Аналітика | Топ-N турнірів за голами за матч (`min_matches`) | агрегація + `$lookup` довідника після `$group` |
| 7 | Телеметрія | Останні запити, зведення за вікном часу | індекс за `timestamp`, `$facet` |
| — | Запис | Атомарний запис виступу | один `update` з конвеєром: додати виступ, оновити лічильники й середній рейтинг |

Атомарний запис ідемпотентний: фільтр містить умову `appearances.appearance_id: {$ne: id}`, тож
повторне надсилання того самого виступу повертає `DUPLICATE` і не змінює лічильники; відсутній
гравець дає `PLAYER_NOT_FOUND`.

## Запуск

Потрібні **Python 3.10+** (розроблено на 3.14) та **Docker Desktop**.

1. Додайте в файл `hosts` (Windows: `C:\Windows\System32\drivers\etc\hosts`) рядок:

   ```
   127.0.0.1 mongo1 mongo2 mongo3
   ```

2. Клонування та залежності:

   ```powershell
   git clone https://github.com/ArthurBarsukov/db_coursework_football.git
   cd db_coursework_football
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Запустіть кластер MongoDB (replica set `rs0`):

   ```powershell
   docker compose up -d
   ```

   Дані зберігаються в томах Docker. Не використовуйте `docker compose down -v` — воно їх видалить.

4. Підготуйте базу, **у цьому порядку**:

   ```powershell
   python -m scripts.setup_indexes --stage pre    # унікальні індекси
   python -m scripts.etl_load_mongo               # завантаження датасету
   python -m scripts.setup_indexes --stage post   # індекси для читання
   ```

   Файли датасету Transfermarkt (`players.csv`, `appearances.csv`, `clubs.csv`, `competitions.csv`)
   не входять до репозиторію (`data/` і `*.csv` у `.gitignore`): покладіть їх у папку `data/` або
   вкажіть іншу через `--data-dir`. Розмір пакета `insert_many` задається через `--batch-size`
   (за замовчуванням 5000). ETL можна запускати повторно: він очищає колекції через
   `delete_many` (індекси зберігаються), а `search_queries` залишає порожньою.

5. Наскрізна перевірка (досьє, запис виступу, фільтрація, аналітика):

   ```powershell
   python main.py
   ```

   Скрипт додає й повторює тестовий виступ `DEMO_STAGE3_APP_1` для гравця `player_id=10`
   (Miroslav Klose). Перед замірами перезавантажте дані повторним запуском ETL.

6. Демонстрація, плани виконання та заміри (запис у `demo_results.txt`):

   ```powershell
   $env:PYTHONUTF8=1; python -m scripts.demo_mongo_queries
   ```

   Файл `demo_results.txt` не потрапляє в Git (`demo_results*.txt` у `.gitignore`), як і `.env`.

## Результати замірів

Середовище: replica set із 3 контейнерів Docker на одній машині, для кожного вузла кеш WiredTiger
обмежено 0,5 ГБ (`--wiredTigerCacheSizeGB 0.5`). Методика: 1 прогрів і 15
повторів, медіана. Телеметрію під час замірів вимкнено (`enable_telemetry=False`).
Абсолютні значення залежать від машини; мають значення співвідношення.

| Запит | Медіана |
|---|---|
| Досьє гравця (`player_id=10`, 20 виступів) | ≈ 2,6 мс |
| Фільтр ESR (Attack + Centre-Forward + вартість) | ≈ 1,0 мс |
| `$elemMatch` (Midfield, L1, ≥60 хв, ≥1 гол), індекс із `competition_id` | ≈ 12,5 мс |
| Те саме з індексом `pos_subpos_value` (базова лінія) | ≈ 90,6 мс |
| Склад клубу (`club_id=398`) | ≈ 2,8 мс |
| Картки захисників за амплуа | десятки мс (≈ 37 мс) |
| Топ-10 турнірів за голами за матч | ≈ 2,06 с (повний прохід по всіх виступах) |
| Останні 20 запитів телеметрії | ≈ 0,6 мс |

Фільтрація за `$elemMatch` до й після додавання `appearances.competition_id` першим ключем
індексу: переглянуто документів **14 272 → 1 164**, час **≈ 90 мс → ≈ 12,5 мс** (≈ 7×).

Читання з primary (`majority`) і з secondary (`local`) на одній машині відрізняється в межах
похибки (≈ 0,7 мс); різниця проявляється лише за мережевого розділення вузлів.

## Поточний стан і план

| Елемент | Стан |
|---|---|
| Інфраструктура: replica set `rs0`, конфігурація, `.env.example` | є |
| Предметна логіка (`domain.py`), індекси, ETL | є |
| DTO, порти, `MongoAdapter`, сервіси, контролери, `main.py` | є |
| Демонстрація, explain-плани, заміри | є |
| Neo4j / Redis / Elasticsearch / Kafka | заглушки (порти й адаптери створені) |
| Повнотекстовий пошук і `build_full_scouting_report` | заплановано (Elasticsearch) |
| Вхідні порти (`ports/input.py`) | заплановано |
| High-Load генератор, веб-моніторинг | заплановано |
