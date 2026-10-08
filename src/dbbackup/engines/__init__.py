from ..config import Target
from .base import Connection, Engine, EngineError
from .mariadb import MariaDBEngine
from .mongodb import MongoDBEngine
from .postgres import PostgresEngine

ENGINES: dict[str, type[Engine]] = {
    "postgres": PostgresEngine,
    "mariadb": MariaDBEngine,
    "mongodb": MongoDBEngine,
}


def engine_for(target: Target, conn: Connection | None = None) -> Engine:
    return ENGINES[target.type](target, conn)


__all__ = ["Connection", "Engine", "EngineError", "engine_for", "ENGINES"]
