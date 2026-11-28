"""
Крок 6: перевірка коректності результату Spark.

Порівнює підсумок з results/spark_result.json (map-reduce по шардах)
з агрегацією MongoDB, виконаною через mongos над усією колекцією.

Критерії збігу по кожній позиції:
    players, goals, matches - точний збіг;
    avg_score, goals_per_match - збіг з похибкою 1e-9.
Додатково: збігається набір позицій і загальна кількість документів.

Запуск (усередині контейнера spark):
    python /app/verify.py
Код завершення: 0 - усе збіглося, 1 - є розбіжності.
"""

import json
import math
import os
import sys

from pymongo import MongoClient

# ============================== НАЛАШТУВАННЯ ==============================
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongos:27017")
DB_NAME = "football_db"
COLLECTION_NAME = "players"
RESULT_PATH = "/app/results/spark_result.json"
TOLERANCE = 1e-9
# ==========================================================================

# Та сама нормалізація позиції, що й у Spark (row.position or "Unknown"):
# відсутня, null та порожній рядок -> "Unknown".
POSITION_EXPR = {
    "$let": {
        "vars": {"p": {"$ifNull": ["$position", ""]}},
        "in": {"$cond": [{"$eq": ["$$p", ""]}, "Unknown", "$$p"]},
    }
}

PIPELINE = [
    {
        "$group": {
            "_id": POSITION_EXPR,
            "players": {"$sum": 1},
            "goals": {"$sum": "$total_goals"},      # null ігнорується, як "or 0" у Spark
            "matches": {"$sum": "$total_matches"},
            # $avg ігнорує null, тобто дорівнює sum_score / scored у Spark
            "avg_score": {"$avg": "$avg_performance_score"},
        }
    }
]


def close(a, b) -> bool:
    """Порівняння чисел з урахуванням None."""
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=0.0, abs_tol=TOLERANCE)


def mongo_reference(collection) -> dict:
    """Еталонний результат: агрегація через mongos."""
    reference = {}
    for doc in collection.aggregate(PIPELINE):
        goals, matches = doc["goals"], doc["matches"]
        reference[doc["_id"]] = {
            "players": doc["players"],
            "goals": goals,
            "matches": matches,
            "goals_per_match": goals / matches if matches else None,
            "avg_score": doc["avg_score"],
        }
    return reference


def main() -> int:
    with open(RESULT_PATH, encoding="utf-8") as f:
        spark_result = json.load(f)
    spark_rows = {row["position"]: row for row in spark_result["final"]}

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    try:
        collection = client[DB_NAME][COLLECTION_NAME]
        mongo_rows = mongo_reference(collection)
        total_docs = collection.count_documents({})
    finally:
        client.close()

    errors = 0

    # 1. Набір позицій
    only_spark = set(spark_rows) - set(mongo_rows)
    only_mongo = set(mongo_rows) - set(spark_rows)
    if only_spark or only_mongo:
        errors += 1
        print(f"РОЗБІЖНІСТЬ у наборі позицій. Лише Spark: {sorted(only_spark)}; "
              f"лише MongoDB: {sorted(only_mongo)}")

    # 2. Порівняння по кожній позиції
    print(f"{'Позиція':<14}{'players':>9}{'goals':>8}{'matches':>9}"
          f"{'goals/match':>13}{'avg_score':>11}  Статус")
    for position in sorted(set(spark_rows) & set(mongo_rows)):
        s, m = spark_rows[position], mongo_rows[position]
        ok = (
            s["players"] == m["players"]
            and s["goals"] == m["goals"]
            and s["matches"] == m["matches"]
            and close(s["goals_per_match"], m["goals_per_match"])
            and close(s["avg_score"], m["avg_score"])
        )
        if not ok:
            errors += 1
        gpm = f"{s['goals_per_match']:.4f}" if s["goals_per_match"] is not None else "-"
        avg = f"{s['avg_score']:.4f}" if s["avg_score"] is not None else "-"
        print(f"{position:<14}{s['players']:>9}{s['goals']:>8}{s['matches']:>9}"
              f"{gpm:>13}{avg:>11}  {'OK' if ok else 'РОЗБІЖНІСТЬ'}")
        if not ok:
            print(f"    Spark:   {s}")
            print(f"    MongoDB: {m}")

    # 3. Загальна кількість документів
    spark_total = sum(row["players"] for row in spark_result["final"])
    shards_total = sum(sh["docs"] for sh in spark_result["shards"])
    print(f"\nДокументів: Spark (підсумок) = {spark_total}, "
          f"Spark (сума шардів) = {shards_total}, MongoDB через mongos = {total_docs}")
    if not (spark_total == shards_total == total_docs):
        errors += 1
        print("РОЗБІЖНІСТЬ у загальній кількості документів")

    print("\nПЕРЕВІРКУ ПРОЙДЕНО: результат Spark збігається з MongoDB."
          if errors == 0 else f"\nПЕРЕВІРКУ НЕ ПРОЙДЕНО: розбіжностей - {errors}.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())