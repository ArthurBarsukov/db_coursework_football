# Football Scouting — поліглотна система скаутингу (`command_service`)

Каркас програмної архітектури курсової роботи з дисципліни **«Бази даних та інформаційні системи»**
(КПІ ім. Ігоря Сікорського, ФПМ, група КМ-42).

Тема: *«Проєктування та розробка розподіленої високонавантаженої системи збору, збереження,
повнотекстового пошуку та моніторингу інформації»* на даних футбольного скаутингу
(відкритий датасет [Football Data from Transfermarkt](https://www.kaggle.com/datasets), Kaggle).


## Зміст

- Архітектурний підхід
- Схема зв'язків
- Структура репозиторію
- Компоненти
- Порти та адаптери
- DTO
- Як проходить запит
- Запуск
- Поточний стан і план

---

## Архітектурний підхід

| Підхід | Як застосований |
|---|---|
| **Гексагональна архітектура** (Ports & Adapters) | Ядро (`application`) залежить лише від інтерфейсів-портів. Конкретні бази даних сховані в адаптерах. |
| **Шарувата архітектура** | Контролер → Фасад → Сервіс → Репозиторій (порт) → БД. Спілкування тільки зверху вниз. |
| **Facade** | `PlayerFacade` збирає три сервіси в одну відповідь (досьє гравця). |
| **Repository** | Кожен сервіс працює зі своїм репозиторієм-портом. |
| **CQRS** | Окремі контролери для читання (`PlayerQueryController`) і запису (`AppearanceCommandController`). |
| **DTO** | Прості `@dataclass`-класи для передачі даних між шарами (`dto.py`). |
| **Polyglot Persistence** | MongoDB, Neo4j, Elasticsearch, Redis, Kafka: кожна технологія під свій профіль навантаження. |

Чому так: щоб можна було замінити технологію (наприклад, MongoDB на PostgreSQL), змінивши тільки
адаптер і не торкаючись бізнес-логіки.

## Структура репозиторію

```
.
├── main.py                              # точка збирання: створює адаптери, сервіси, фасад, контролери
└── command_service/
    ├── dto.py                           # DTO (передача даних між шарами)
    ├── ports/
    │   └── output.py                    # вихідні порти (Protocol-інтерфейси)
    ├── application/
    │   └── services.py                  # сервіси та PlayerFacade (ядро)
    └── adapters/
        ├── inbound/
        │   └── controllers.py           # вхідні адаптери (контролери)
        └── outbound/
            ├── mongo_adapter.py         # MongoDB
            ├── neo4j_adapter.py         # Neo4j
            ├── redis_adapter.py         # Redis
            ├── elastic_adapter.py       # Elasticsearch
            └── kafka_adapter.py         # Apache Kafka
```

## Компоненти

### Контролери (`adapters/inbound/controllers.py`)

| Клас | Метод | Що робить |
|---|---|---|
| `PlayerQueryController` | `get_player(player_id)` | Запит на читання: викликає фасад, повертає `{"status": "success", "data": ...}` або `{"status": "error", ...}`. |
| `AppearanceCommandController` | `record_match_appearance(request_data)` | Команда запису: перетворює словник на `AppearanceDTO` і передає на збереження. |

### Сервіси та фасад (`application/services.py`)

| Клас | Залежить від | Метод | Призначення |
|---|---|---|---|
| `PlayerProfileService` | `PlayerProfileRepositoryPort` | `fetch_player_info` | Профіль гравця |
| `AppearanceService` | `AppearanceRepositoryPort` | `fetch_player_statistics` | Виступи гравця в матчах |
| `ScoutingService` | `GraphRepositoryPort` | `find_analogous_players` | Гравці-аналоги (граф) |
| `PlayerFacade` | три сервіси вище | `get_full_player_dossier` | Збирає `PlayerDossierDTO` |

## Порти та адаптери

| Порт (`ports/output.py`) | Методи | Адаптер | Технологія | Призначення |
|---|---|---|---|---|
| `PlayerProfileRepositoryPort` | `get_player_by_id`, `search_players_by_query` | `MongoAdapter` | MongoDB | Профілі гравців |
| `AppearanceRepositoryPort` | `get_appearances_by_player`, `save_appearance` | `MongoAdapter` | MongoDB | Журнал виступів |
| `GraphRepositoryPort` | `find_tactical_analogs`, `get_club_with_roster` | `Neo4jAdapter` | Neo4j | Кар'єрні зв'язки, аналоги |
| `CacheStorePort` | `get_cached_dossier`, `set_cached_dossier` | `RedisAdapter` | Redis | Кеш досьє (TTL) |
| `SearchIndexPort` | `search_by_text` | `ElasticsearchAdapter` | Elasticsearch | Повнотекстовий пошук за скаутським звітом |
| `EventPublisherPort` | `publish_appearance_event` | `KafkaPublisherAdapter` | Kafka | Асинхронна черга запису |
| `TelemetryRepositoryPort` | `log_query_metrics` | *(ще немає)* | — | Метрики запитів |

## DTO

| Клас | Призначення |
|---|---|
| `PlayerDTO` | Профіль гравця: позиція, нога, громадянство, клуб, ринкова вартість, агреговані показники |
| `AppearanceDTO` | Один вихід на поле: хвилини, голи, асисти, картки, `performance_score` |
| `PlayerWithAppearancesDTO` | `PlayerDTO` + список `AppearanceDTO` |
| `PlayerDossierDTO` | `PlayerWithAppearancesDTO` + `tactical_analogs` (результат фасаду) |
| `ClubDTO`, `CompetitionDTO` | Клуб і змагання |
| `SearchQueryDTO` | Метрики пошукового запиту: кеш-хіт, затримка |

## Як проходить запит

**Читання** (`get_player(1)`):

1. `PlayerQueryController` викликає `PlayerFacade.get_full_player_dossier(1)`.
2. Фасад послідовно звертається до `PlayerProfileService`, `AppearanceService`, `ScoutingService`.
3. Кожен сервіс викликає свій порт; фактично працює `MongoAdapter` або `Neo4jAdapter`.
4. Фасад збирає `PlayerDossierDTO`, контролер повертає його у вигляді словника.

**Запис** (`record_match_appearance`):

1. `AppearanceCommandController` створює `AppearanceDTO` із вхідного словника.
2. Передає його в `AppearanceService`, а той у `AppearanceRepositoryPort.save_appearance`.
3. Кінцева мета: замість прямого запису публікувати подію через `EventPublisherPort` (Kafka), щоб
   згладити пікові навантаження (див. розділ 3.3 звіту).

## Запуск

Потрібен **Python 3.10+** (у коді використовується запис `dict | None`). Зовнішніх залежностей немає.

```bash
git clone https://github.com/ArthurBarsukov/db_coursework_football.git
cd db_coursework_football
python main.py
```

Поки адаптери є заглушками, `main.py` покаже, що ядро збирається коректно, а тестовий запит
завершиться відповіддю з `"status": "error"` (адаптер кидає `NotImplementedError`). Це очікувана поведінка.

## Поточний стан і план

| Елемент | Стан |
|---|---|
| Контролери, сервіси, фасад, порти, адаптери, DTO | є (заглушки) |
| Mongo / Neo4j адаптери підключені до сервісів | так |
| Redis / Elasticsearch / Kafka підключені до сервісів | ще ні (порти й адаптери створені, сервісів для них нема) |
| Повний CRUD у репозиторіях | частково (є read і `save_appearance`; `update` та `delete` — заплановано) |
| Вхідні порти (`ports/input.py`) | заплановано |
| Реалізація адаптерів (реальні запити до БД) | заплановано |
| High-Load генератор, ETL-конвеєр, веб-моніторинг | заплановано |
