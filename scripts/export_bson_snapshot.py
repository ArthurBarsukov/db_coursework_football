"""Export the current configured MongoDB ETL output as a streaming BSON snapshot."""
import argparse
from pathlib import Path

from command_service.config import create_mongo_client, get_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    names = ('players', 'clubs', 'competitions')
    if any((args.directory / (name + '.bson')).exists() for name in names):
        raise FileExistsError('Snapshot files already exist; choose a new directory.')
    client = create_mongo_client()
    try:
        db = get_database(client)
        for name in names:
            with (args.directory / (name + '.bson')).open('wb') as output:
                for batch in db[name].find_raw_batches({}, batch_size=100):
                    output.write(batch)
            print(f'{name}: {db[name].count_documents({})} exported', flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
