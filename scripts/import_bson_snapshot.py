"""Low-memory import of an existing ETL snapshot into an EMPTY MongoDB database.

Use when the full pandas ETL exceeds container memory. Snapshots must come from
the project's completed ETL, not raw CSV. This utility never clears collections.
"""
import argparse
from pathlib import Path

from bson import decode_file_iter

from command_service.config import create_mongo_client, get_database, get_critical_collection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    names = ('players', 'clubs', 'competitions')
    for name in names:
        if not (args.directory / (name + '.bson')).is_file():
            raise FileNotFoundError(name + '.bson')
    client = create_mongo_client()
    try:
        db = get_database(client)
        if any(db[name].count_documents({}) for name in names):
            raise RuntimeError('Destination must be empty. No documents were deleted or replaced.')
        for name in names:
            collection = get_critical_collection(db, name)
            count, batch = 0, []
            with (args.directory / (name + '.bson')).open('rb') as source:
                for doc in decode_file_iter(source):
                    batch.append(doc)
                    if len(batch) == 100:
                        collection.insert_many(batch, ordered=True)
                        count += len(batch)
                        batch = []
                if batch:
                    collection.insert_many(batch, ordered=True)
                    count += len(batch)
            print(f'{name}: {count} imported', flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
