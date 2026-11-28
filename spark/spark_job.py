"""
Завдання 14: збір даних одного запиту з різних шардів через Spark (map-reduce).

Запит: агрегати гравців за позиціями (position):
    кількість гравців, сума голів, сума матчів, голи за матч, середня оцінка.

Схема роботи:
    1. Крок 4: читаємо колекцію football_db.players з КОЖНОГО шарда окремо
       (напряму, повз mongos) і об'єднуємо в один DataFrame з колонкою shard.
    2. Крок 5 (map):    кожен документ -> пара ((shard, position), (суми, лічильники)).
    3. Крок 5 (reduce): reduceByKey складає суми спочатку по (shard, position)
                        (часткові результати шардів), потім по position (підсумок).
    4. Результат (підсумок + внесок кожного шарда + час) пишемо у JSON,
       який читає вебінтерфейс.

Запуск (усередині контейнера spark):
    python /app/spark_job.py
"""

import json
import math
import os
import time
from datetime import datetime
from functools import reduce

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import lit

# ============================== НАЛАШТУВАННЯ ==============================
DB_NAME = "football_db"
COLLECTION_NAME = "players"
OUTPUT_PATH = "/app/results/spark_result.json"   # ./spark/results на хості
QUERY_TITLE = "Агрегати гравців за позиціями"

# Адреси шардів у внутрішній мережі Docker (mongo-sharded-net).
# Підключаємось до replica set кожного шарда напряму, а не через mongos,
# щоб бачити, скільки даних зберігає кожен шард.
SHARDS = {
    "shard1RS": "mongodb://shard-1:27018/?replicaSet=shard1RS",
    "shard2RS": "mongodb://shard-2:27018/?replicaSet=shard2RS",
    "shard3RS": "mongodb://shard-3:27018/?replicaSet=shard3RS",
}

# $project забирає лише 5 потрібних полів. Без нього Spark тягнув би весь
# документ разом з масивом appearances (~420 МіБ у сумі).
PIPELINE = (
    '[{"$project": {"_id": 0, "player_id": 1, "position": 1, '
    '"total_goals": 1, "total_matches": 1, "avg_performance_score": 1}}]'
)
# ==========================================================================


def build_spark() -> SparkSession:
    """Створює локальну Spark-сесію з MongoDB Spark Connector."""
    spark = (
        SparkSession.builder.appName("players-by-position")
        .master("local[*]")  # усі ядра контейнера
        .config(
            "spark.jars.packages",
            # Конектор для Spark 3.x / Scala 2.12; jar завантажується
            # з Maven Central при першому запуску (кешується у томі spark_ivy).
            "org.mongodb.spark:mongo-spark-connector_2.12:10.4.0",
        )
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")  # менше службових логів
    return spark


def read_shard(spark: SparkSession, name: str, uri: str) -> DataFrame:
    """Читає колекцію players з одного шарда й додає колонку shard."""
    return (
        spark.read.format("mongodb")
        .option("connection.uri", uri)
        .option("database", DB_NAME)
        .option("collection", COLLECTION_NAME)
        .option("aggregation.pipeline", PIPELINE)
        .load()
        .withColumn("shard", lit(name))  # звідки прийшов рядок
    )


# ------------------------------- MAP / REDUCE -------------------------------
def to_pair(row):
    """
    MAP: один документ гравця -> (ключ, значення).

    Ключ:     (shard, position)
    Значення: (гравців, голи, матчі, сума оцінок, скільки гравців мають оцінку)

    У значенні лежать суми й лічильники, а не середні: тільки їх можна
    коректно складати по шардах (reduce має бути асоціативним і комутативним).
    """
    score = row.avg_performance_score
    # У гравців без виступів оцінка null (або NaN) - їх не враховуємо у середній.
    has_score = score is not None and not (
        isinstance(score, float) and math.isnan(score)
    )
    return (
        (row.shard, row.position or "Unknown"),
        (
            1,                                   # гравців
            row.total_goals or 0,                # голи
            row.total_matches or 0,              # матчі
            float(score) if has_score else 0.0,  # сума оцінок
            1 if has_score else 0,               # кількість гравців з оцінкою
        ),
    )


def add(a, b):
    """REDUCE: поелементне додавання двох кортежів сум."""
    return tuple(x + y for x, y in zip(a, b))


def main() -> None:
    spark = build_spark()

    # --- Крок 4: зчитування з кожного шарда й об'єднання ---
    players = reduce(
        DataFrame.unionByName,
        [read_shard(spark, name, uri) for name, uri in SHARDS.items()],
    )

    # --- Крок 5: map-reduce ---
    mapped = players.rdd.map(to_pair)

    # Рівень 1: часткові результати (shard, position) -> суми на шарді.
    # cache(), щоб не читати шарди з MongoDB вдруге для підсумку.
    partial = mapped.reduceByKey(add).cache()

    # Рівень 2: підсумок position -> загальні суми по всіх шардах.
    final_kv = partial.map(lambda kv: (kv[0][1], kv[1])).reduceByKey(add)

    # collect() запускає обчислення; міряємо час від читання до готового результату.
    t0 = time.perf_counter()
    partials = partial.collect()
    final = final_kv.collect()
    duration = time.perf_counter() - t0

    # --- Формування результату ---
    # Кількість документів на шарді = сума гравців у його часткових результатах
    # (окремий запит count не потрібен).
    docs_per_shard = {name: 0 for name in SHARDS}
    for (shard, _position), values in partials:
        docs_per_shard[shard] += values[0]

    partials_out = [
        {
            "shard": shard,
            "position": position,
            "players": v[0],
            "goals": v[1],
            "matches": v[2],
        }
        for (shard, position), v in sorted(partials)
    ]

    final_out = []
    for position, (n, goals, matches, score_sum, scored) in sorted(
        final, key=lambda kv: -kv[1][0]  # від найчисленнішої позиції
    ):
        final_out.append(
            {
                "position": position,
                "players": n,
                "goals": goals,
                "matches": matches,
                # Середні рахуємо лише тут, після reduce; без округлення,
                # щоб збігалося з агрегацією MongoDB у verify.py.
                "goals_per_match": goals / matches if matches else None,
                "avg_score": score_sum / scored if scored else None,
            }
        )

    result = {
        "query": QUERY_TITLE,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(duration, 2),
        "shards": [
            {"name": name, "docs": docs_per_shard[name]} for name in SHARDS
        ],
        "partials": partials_out,
        "final": final_out,
    }

    # --- Збереження JSON для інтерфейсу ---
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("Документів по шардах:", json.dumps(result["shards"], ensure_ascii=False))
    print("Позицій у підсумку:", len(final_out))
    print("Усього гравців:", sum(r["players"] for r in final_out))
    print("Час виконання, с:", result["duration_sec"])
    print("Результат записано у", OUTPUT_PATH)

    spark.stop()


if __name__ == "__main__":
    main()