# Етап 12 — вебінтерфейс Football Scouting

Реалізовано каталог гравців із фільтрами, сортуванням, пагінацією та переглядом досьє. Дані надходять через наявні контролери, прикладні сервіси та MongoAdapter. Вебзастосунок не виконує записів і не запускає демонстраційний main.py; запис телеметрії для вебзапитів вимкнено.

## Запуск

Потрібні Python 3.10+ (рекомендовано 3.12), Docker Desktop та імпортований датасет. Підготуйте replica set, hosts, .env та ETL за основною інструкцією проєкту. CSV з data.zip розпакуйте в корінь проєкту, щоб отримати data/players.csv тощо. Не перезаписуйте вже налаштований .env.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Лише якщо .env ще немає:
Copy-Item .env.example .env
docker compose up -d
# Лише для першого завантаження, коли кластер готовий:
.\.venv\Scripts\python.exe -m scripts.setup_indexes --stage pre
.\.venv\Scripts\python.exe -m scripts.etl_load_mongo
.\.venv\Scripts\python.exe -m scripts.setup_indexes --stage post
.\.venv\Scripts\python.exe web_app.py
```

Відкрийте http://127.0.0.1:5000/. Зупинка вебсервера: Ctrl+C. Вебсервер Flask призначений для локальної демонстрації.

Якщо порт 5000 зайнятий або недоступний, використайте інший порт:

```powershell
.\.venv\Scripts\python.exe -m flask --app web_app:create_app run --host 127.0.0.1 --port 8765
```

У такому разі відкрийте http://127.0.0.1:8765/. При недоступності MongoDB сторінка залишається доступною, а запит повертає 503 з повідомленням і кнопкою повтору. Перевірте Docker, hosts і MONGO_URI. Рядок підключення в config.py та docker-compose.yml не змінено.

## Docker UI без конфлікту локальних портів

Додатковий docker-compose.ui.yml запускає цей самий вебзастосунок у контейнері на http://127.0.0.1:8766/. Три вузли MongoDB спілкуються через внутрішню мережу Docker, тому hosts і вільні порти 27017–27019 на Windows не потрібні. Основний docker-compose.yml не змінено; overlay прибирає лише публікацію портів БД і додає web. Імена контейнерів mongo1, mongo2, mongo3, mongo-init успадковані від основного файлу: перед запуском переконайтеся, що їх не використовує інший стек. Потрібен Compose з підтримкою !reset.

```powershell
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml up -d --build
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml ps
docker exec mongo1 mongosh --quiet --eval "rs.status().members.map(m => ({name: m.name, stateStr: m.stateStr}))"
```

Папка data з чотирма CSV має бути в корені проєкту. Вона монтується в web лише для читання й не потрапляє в образ. Для першого завантаження в порожню БД виконайте наведені команди; ETL очищує наявні записи, тому для вже заповненої БД цей крок пропустіть.

```powershell
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web python -m scripts.setup_indexes --stage pre
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web python -m scripts.etl_load_mongo
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web python -m scripts.setup_indexes --stage post
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web pip install -r requirements-dev.txt
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T -e STAGE12_TEST_MONGO_URI="mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0" -e STAGE12_TEST_DB=football_scouting web python -m unittest discover -s tests -v
```

Зупинити контейнери зі збереженням даних:

```powershell
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml stop
```

## Результат Docker-перевірки 27.11.2026

MongoDB 7.0 у командному replica set rs0: mongo1 PRIMARY, mongo2 і mongo3 SECONDARY, усі healthy. Web працює в тому самому Docker-стеку на 127.0.0.1:8766. Усі 13 тестів пройдено всередині web-контейнера; у браузері перевірено Klose, досьє та другу сторінку виступів. На кожному вузлі є 50149 гравців, 796 клубів і 65 турнірів. SHA-256 BSON-документів, відсортованих за _id, збігається з початковим ETL на всіх трьох вузлах. Вкладених виступів 1894348; індексів players — 5. Failover і навантажувальні вимірювання не виконувалися.

Повний pandas ETL у контейнері перевищив доступну пам’ять (exit 137). Тому для цієї перевірки використано його справжній результат, раніше отриманий на хості, та потокове копіювання BSON пакетами по 100 документів. Оригінальний ETL не змінено. Якщо пам’яті достатньо, можна використовувати звичайний шлях CSV → ETL у контейнері. Для обмеженої пам’яті додано export_bson_snapshot і import_bson_snapshot: експорт читає БД із поточного MONGO_URI/MONGO_DB_NAME, а імпорт відмовляється працювати, якщо будь-яка з трьох цільових колекцій непорожня. Для імпорту потрібен завершений результат оригінального ETL, а не сирі CSV.

```powershell
# На хості, де вже виконано ETL: MONGO_URI/MONGO_DB_NAME вказують на джерело.
.\.venv\Scripts\python.exe -m scripts.export_bson_snapshot data/stage12-seed
# У Docker: MONGO_URI/MONGO_DB_NAME налаштовані overlay на окрему порожню ціль.
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web python -m scripts.import_bson_snapshot /app/data/stage12-seed
docker compose -p stage12verify -f docker-compose.yml -f docker-compose.ui.yml exec -T web python -m scripts.setup_indexes --stage post
```

## Повний флоу запиту до MongoDB

```text
Форма у браузері
 -> GET /api/players
 -> HTTP-маршрут web_app.py
 -> PlayerQueryController.list_players
 -> PlayerProfileService.list_players / filter_players
 -> PlayerProfileRepositoryPort.filter_players
 -> MongoAdapter.filter_players
 -> MongoDB.players.find(...).sort(...).skip(...).limit(...)
 -> PlayerDTO -> Controller -> JSON -> таблиця у браузері
```

Досьє: GET /api/players/<id> → PlayerQueryController.get_player → PlayerFacade → PlayerProfileService → MongoAdapter → MongoDB. Фасад додає довідкові відомості клубу й турнірів. Neo4j поки є заглушкою; попередження tactical_analogs_unavailable показується користувачу, основне досьє працює.

## API

- GET / — сторінка каталогу.
- GET /api/players — список. name: пошук за буквальним підрядком без урахування регістру; position, foot, citizenship та інші поля PlayerFilterCriteria; min_market_value_eur, max_market_value_eur, min_avg_score; sort_by: market_value_eur, avg_performance_score, total_goals, total_matches; sort_order: -1 або 1; limit: 1–100 (типово 20); offset: 0–100000.
- GET /api/players/10?appearances_limit=20&appearances_offset=20 — досьє, 1–100 виступів, offset 0–100000. Відповідь містить pagination: offset, limit, total, has_more. У досьє є кнопки «Назад» / «Далі» та номер сторінки, по 20 виступів. MongoDB повертає потрібний зріз масиву через $slice.

```json
{"status":"success","count":20,"data":[],"pagination":{"offset":0,"limit":20,"has_more":true}}
```

Це схема відповіді: фактичний масив data містить count записів. count означає кількість записів поточної сторінки, а не загальну кількість збігів. Для has_more читається один додатковий документ; другий запит count не потрібен. Рівні значення сортування впорядковуються за player_id. Дати повертаються у форматі ISO 8601, відсутні дані — null. Ім’я екранується як буквальний текст для MongoDB regex; HTML результатів створюється через textContent.

Коди відповіді: 200 — успіх (зокрема порожній список), 400 — помилка параметрів, 404 — гравця немає, 503 — помилка сховища. Драйверні повідомлення та URI не передаються клієнту. Пошук за підрядком не є нечітким повнотекстовим пошуком Elasticsearch і може потребувати оптимізації на великих наборах. Offset-пагінація підходить для цієї демонстрації; при одночасних змінах даних сторінки можуть зміщуватися.

## Перевірка

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Додаткові 3 інтеграційні перевірки на справжньому імпортованому наборі (лише читання):

```powershell
$env:STAGE12_TEST_MONGO_URI = $env:MONGO_URI
$env:STAGE12_TEST_DB = $env:MONGO_DB_NAME
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Без STAGE12_TEST_MONGO_URI ці 3 перевірки пропускаються. Для локальної перевірки використано mongodb://127.0.0.1:27027/?replicaSet=stage12local і football_stage12_local. Усі 13 перевірок пройдено на інтегрованій гілці від main 0de42a8. Інтеграційні тести звіряють пошук, досьє, усі сторінки історії Klose та відбір нападників із реальними документами БД.

Виконано 10 тестів: HTTP-сторінка й ресурси, пагінація при однакових значеннях сортування, буквальний пошук імені, відсутня вартість, порожній список і 404, досьє й ISO-дата, некоректні параметри, недоступність сховища, відсутність запису телеметрії. Тести використовують реальні Controller, Service та MongoAdapter із mongomock замість сервера MongoDB. Вони не перевіряють реплікацію, мережеве підключення або продуктивність.

Браузерна перевірка: каталог, пошук Klose й відкриття досьє перевірено на справжніх записах після ETL. Додатково виконано інтеграцію з установленим локально MongoDB 9.0: окремий одновузловий replica set stage12local на 127.0.0.1:27027, БД football_stage12_local. Завантажено 50149 гравців, 796 клубів, 65 турнірів та 1894348 вкладених виступів. Із 1894350 вхідних виступів 2 відкинуто через відсутність профілю гравця. Перевірено API списку та досьє Miroslav Klose (136 виступів), дати й довідкові відомості. Це підтверджує повний шлях HTTP → Controller → Service → MongoAdapter → MongoDB → UI, не перевіряє failover. Пізніше підтверджено також роботу на Docker replica set rs0 із трьох вузлів (див. результат вище). Контрольний список ручної перевірки:

1. Відкрити каталог, знайти Klose, перевірити дані зі сховищем.
2. Відфільтрувати Attack із максимальною вартістю 15000000.
3. Перейти на наступну сторінку й назад; відкрити досьє.
4. Перевірити відсутнього гравця та невалідні параметри.
5. Зробити скриншоти каталогу, фільтрації та досьє для звіту.

## Файли зміни

Нові: web_app.py, templates/index.html, static/app.css, static/app.js, tests/test_web.py, requirements-dev.txt, STAGE12.md.

Змінені: dto.py (name, offset), mongo_adapter.py (пошук і пагінація), services.py (валідація й читання сторінки), controllers.py (відповідь зі сторінкою), requirements.txt (Flask), .gitignore (локальне середовище Python).

Нові індекси не створюються; записи та схема БД не змінюються. Не додавайте .env або data до Git. Роботу слід інтегрувати в актуальну main у гілці feature/stage-12-ui, перевірити diff і відкрити PR після справжньої перевірки з MongoDB.
