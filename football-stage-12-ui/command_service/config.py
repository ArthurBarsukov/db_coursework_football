import os
from dataclasses import dataclass

from dotenv import load_dotenv
from pymongo import MongoClient, ReadPreference
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

load_dotenv()


@dataclass(frozen=True)
class MongoSettings:
    uri: str
    db_name: str
    server_selection_timeout_ms: int = 30000

    @classmethod
    def from_env(cls) -> "MongoSettings":
        return cls(
            uri=os.getenv(
                "MONGO_URI",
                "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0",
            ),
            db_name=os.getenv("MONGO_DB_NAME", "football_scouting"),
            server_selection_timeout_ms=int(
                os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "30000")
            ),
        )


def create_mongo_client(settings: MongoSettings | None = None) -> MongoClient:
    cfg = settings or MongoSettings.from_env()
    return MongoClient(
        cfg.uri,
        serverSelectionTimeoutMS=cfg.server_selection_timeout_ms,
    )


def get_database(
    client: MongoClient,
    settings: MongoSettings | None = None,
) -> Database:
    cfg = settings or MongoSettings.from_env()
    return client[cfg.db_name]


def get_critical_collection(db: Database, name: str) -> Collection:
    """A collection for critical writes (w='majority') and read-after-write (primary + majority)."""
    return db.get_collection(
        name,
        write_concern=WriteConcern(w="majority", j=True),
        read_preference=ReadPreference.PRIMARY,
        read_concern=ReadConcern("majority"),
    )


def get_read_collection(db: Database, name: str) -> Collection:
    """Collection for reading dossiers, filtering, and aggregations (secondaryPreferred)."""
    return db.get_collection(
        name,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
        read_concern=ReadConcern("local"),
    )


def get_telemetry_collection(
    db: Database,
    name: str = "search_queries",
) -> Collection:
    """Collection for fast writing of telemetry logs (w=1)."""
    return db.get_collection(
        name,
        write_concern=WriteConcern(w=1),
        read_preference=ReadPreference.PRIMARY,
    )