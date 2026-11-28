import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import bson
from bson import json_util
from pymongo import MongoClient

# ================= НАЛАШТУВАННЯ БЕНЧМАРКУ (3 ШАРДИ) =================
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "football_db"
COLLECTION_NAME = "players"
EXPECTED_SHARDS = 3

DATA_FILE_PATH = Path("data/stage12-seed/players.bson")

BATCH_SIZE = 1000   # Ідентичний розмір пакета
NUM_WORKERS = 8     # Ідентична кількість паралельних потоків
NUM_RUNS = 3        # Кількість прогонів для усереднення
# ====================================================================


def load_documents(file_path: Path) -> list[dict[str, Any]]:
    """Завантажує очищені BSON/JSON документи із диска в пам'ять."""
    if not file_path.exists():
        raise FileNotFoundError(
            f"Файл '{file_path}' не знайдено. Перевірте шлях у DATA_FILE_PATH."
        )

    suffix = file_path.suffix.lower()
    if suffix == ".bson":
        with open(file_path, "rb") as f:
            docs = bson.decode_all(f.read())
    elif suffix in {".jsonl", ".ndjson"}:
        with open(file_path, "r", encoding="utf-8") as f:
            docs = [json_util.loads(line) for line in f if line.strip()]
    elif suffix == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            docs = json_util.loads(f.read())
    else:
        raise ValueError(f"Непідтримуваний формат файлу: {suffix}")

    if not isinstance(docs, list) or not docs:
        raise ValueError("Файл не містить непорожнього списку документів.")

    if "player_id" not in docs[0]:
        raise KeyError("У документах відсутнє поле 'player_id' для shard key.")

    return docs


def verify_cluster_setup(client: MongoClient, expected_shards: int) -> list[str]:
    """Перевіряє ручне налаштування 3 шардів та шардування колекції без зміни стану БД."""
    admin_db = client["admin"]
    namespace = f"{DB_NAME}.{COLLECTION_NAME}"

    shards = admin_db.command("listShards").get("shards", [])
    shard_ids = [s["_id"] for s in shards]
    if len(shards) != expected_shards:
        raise RuntimeError(
            f"Очікувалося {expected_shards} активних шардів, але знайдено {len(shards)}: {shard_ids}. "
            "Додайте всі шарди вручну через mongosh перед запуском тесту."
        )

    col_meta = client["config"]["collections"].find_one({"_id": namespace})
    if not col_meta or col_meta.get("dropped"):
        raise RuntimeError(
            f"Колекція '{namespace}' не шардована. "
            "Виконайте вручну sh.enableSharding() та sh.shardCollection() у mongosh."
        )

    if col_meta.get("key") != {"player_id": "hashed"}:
        raise RuntimeError(
            f"Невірний shard key для '{namespace}': {col_meta.get('key')}. "
            "Очікувався ключ {'player_id': 'hashed'}."
        )

    print(f"      Перевірку пройдено: активні шарди {shard_ids}, ключ шардингу {col_meta.get('key')}.")
    return shard_ids


def chunk_list(data: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    """Розбиває список документів на батчі фіксованого розміру."""
    return [data[i : i + batch_size] for i in range(0, len(data), batch_size)]


def insert_batch(collection, batch: list[dict[str, Any]]) -> tuple[int, int]:
    """Вставляє один пакет документів у невпорядкованому режимі."""
    collection.insert_many(batch, ordered=False)
    appearances_count = sum(len(doc.get("appearances", [])) for doc in batch)
    return len(batch), appearances_count


def print_shard_distribution(client: MongoClient) -> None:
    """Виводить розподіл документів та обсягу даних по трьох шардах."""
    db = client[DB_NAME]
    stats = list(db[COLLECTION_NAME].aggregate([{"$collStats": {"storageStats": {}}}]))

    print("\n--- Розподіл даних по 3 шардах (після 3-го прогону) ---")
    total_docs = sum(s["storageStats"]["count"] for s in stats)
    for entry in sorted(stats, key=lambda x: x["shard"]):
        shard_name = entry["shard"]
        count = entry["storageStats"]["count"]
        size_mb = entry["storageStats"]["size"] / (1024 * 1024)
        pct = (count / total_docs * 100) if total_docs else 0.0
        print(
            f"  {shard_name:<10} | Документів: {count:>6} ({pct:5.1f}%) | Розмір BSON: {size_mb:6.2f} MB"
        )


def run_benchmark() -> None:
    print(f"[1/4] Читання даних із '{DATA_FILE_PATH}'...")
    documents = load_documents(DATA_FILE_PATH)
    batches = chunk_list(documents, BATCH_SIZE)
    total_profiles = len(documents)
    total_appearances = sum(len(d.get("appearances", [])) for d in documents)
    print(
        f"      Завантажено {total_profiles:,} профілів ({total_appearances:,} виступів), "
        f"{len(batches)} батчів по {BATCH_SIZE}."
    )

    client = MongoClient(MONGO_URI, maxPoolSize=NUM_WORKERS * 2)
    try:
        print("[2/4] Перевірка ручної конфігурації кластера (очікується 3 шарди)...")
        shard_ids = verify_cluster_setup(client, EXPECTED_SHARDS)
        collection = client[DB_NAME][COLLECTION_NAME]

        print(f"[3/4] Запуск {NUM_RUNS} прогонів запису ({NUM_WORKERS} потоків)...")
        run_metrics: list[dict[str, float]] = []

        for run_idx in range(1, NUM_RUNS + 1):
            # Очищаємо лише документи перед прогоном, не змінюючи розподіл чанків по 3 шардах
            collection.delete_many({})

            inserted_docs = 0
            inserted_appearances = 0

            t_start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
                futures = [
                    executor.submit(insert_batch, collection, batch) for batch in batches
                ]
                for future in as_completed(futures):
                    docs_cnt, app_cnt = future.result()
                    inserted_docs += docs_cnt
                    inserted_appearances += app_cnt
            t_end = time.perf_counter()

            elapsed = t_end - t_start
            docs_per_sec = inserted_docs / elapsed
            appearances_per_sec = inserted_appearances / elapsed

            run_metrics.append(
                {
                    "elapsed": elapsed,
                    "docs_per_sec": docs_per_sec,
                    "appearances_per_sec": appearances_per_sec,
                }
            )
            print(
                f"  Прогін {run_idx}/{NUM_RUNS}: час = {elapsed:.4f} с | "
                f"{docs_per_sec:,.1f} docs/sec | {appearances_per_sec:,.1f} appearances/sec"
            )

        avg_time = sum(m["elapsed"] for m in run_metrics) / NUM_RUNS
        avg_docs_sec = sum(m["docs_per_sec"] for m in run_metrics) / NUM_RUNS
        avg_app_sec = sum(m["appearances_per_sec"] for m in run_metrics) / NUM_RUNS

        print("\n=========== ПІДСУМКОВІ МЕТРИКИ (3 ШАРДИ) ===========")
        print(f"Активних шардів:                  {len(shard_ids)} ({', '.join(shard_ids)})")
        print(f"Параметри навантаження:           {NUM_WORKERS} потоків, батч {BATCH_SIZE}")
        print(f"Профілів / виступів за прогін:    {total_profiles:,} / {total_appearances:,}")
        print(f"Середній час запису (3 прогони):  {avg_time:.4f} с")
        print(f"Середня швидкість (профілі):      {avg_docs_sec:,.1f} docs/sec")
        print(f"Середня швидкість (виступи):      {avg_app_sec:,.1f} appearances/sec")
        print("=====================================================")

        print("[4/4] Перевірка балансування між 3 шардами...")
        print_shard_distribution(client)
    finally:
        client.close()


if __name__ == "__main__":
    run_benchmark()