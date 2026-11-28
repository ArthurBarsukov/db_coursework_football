# Етап 15 — кешування досьє в Redis

MongoDB залишається джерелом даних. Сервіс CachedDossiers виконує cache-aside:
MISS → MongoDB → Redis (TTL 300 с); HIT → Redis без запиту до MongoDB.
Кешуються лише успішні відповіді досьє, разом із пагінацією. Ключ містить
версію, простір імен, player_id, appearances_limit та appearances_offset.
Дати серіалізуються як ISO 8601. Показники поточного запиту не кешуються.

## Запуск у PowerShell

Використовуйте налаштовані MongoDB, hosts, .env і датасет із попередніх етапів.
Не перезаписуйте наявний .env і не запускайте ETL повторно без потреби.
Відкрийте Docker Desktop, потім у корені проєкту:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose -f docker-compose.redis.yml up -d
docker compose -f docker-compose.redis.yml exec redis redis-cli ping
.\.venv\Scripts\python.exe web_app.py
```

Якщо середовища .venv ще немає: `python -m venv .venv`.
Відкрийте http://127.0.0.1:5000/ і залиште термінал сервера відкритим.

Додайте до наявного .env за потреби (не дублюйте існуючі параметри):

```dotenv
REDIS_ENABLED=1
REDIS_URL=redis://127.0.0.1:6379/0
CACHE_TTL_SECONDS=300
REDIS_CACHE_NAMESPACE=football:dossier:v1
```

Якщо вебсервер працює в Docker, а не на хості, адреса Redis повинна бути
`redis://redis:6379/0`; обидва сервіси мають бути в одній мережі Compose.
Для різних MongoDB-датасетів використовуйте різні REDIS_CACHE_NAMESPACE.
Для шардованого MongoDB з етапу 13 збережіть коректні MONGO_URI та MONGO_DB_NAME;
цей етап не змінює конфігурацію MongoDB чи Spark.

## Демонстрація

1. Відкрийте досьє гравця: джерело MongoDB, промах кешу.
2. Натисніть «Повторити запит»: джерело Redis, влучання в кеш.
3. Перейдіть на іншу сторінку виступів: вона має окремий ключ кешу.
4. Для демонстрації TTL задайте CACHE_TTL_SECONDS=10, перезапустіть вебсервер
   і відкрийте ще не кешоване досьє. Через 10 секунд повторіть запит: знову MISS.
   Нова тривалість TTL застосовується до нових записів, не до вже збережених.
5. Зупиніть Redis: `docker compose -f docker-compose.redis.yml stop redis`.
   Досьє завантажиться з MongoDB з повідомленням про недоступність Redis.
6. Відновіть: `docker compose -f docker-compose.redis.yml start redis`.

Час на сторінці — тривалість серверної обробки досьє, включно зі зверненням
до кешу; це не час мережі/браузера і не гарантія прискорення кожного запиту.
Зміни в MongoDB можуть бути невидимі до завершення TTL. Вебінтерфейс лише читає
дані; після зовнішнього імпорту змініть namespace або дочекайтеся завершення TTL.

## Перевірки

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

tests/test_cache.py використовує fakeredis і mongomock: перевіряє HIT/MISS,
обхід MongoDB при HIT, TTL/видалення простроченого запису, пагінацію, ISO-дати,
ізоляцію namespace, пошкоджені записи, відмови читання/запису Redis та помилки БД.
Ці тести не замінюють демонстрацію з реальними MongoDB і Redis.
