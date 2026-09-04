"""Database lifecycle helpers."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Database:
    """Own an engine and short-lived SQLAlchemy sessions."""

    def __init__(self, url: str):
        engine_options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"timeout": 30}
        if url == "sqlite+pysqlite:///:memory:":
            engine_options.update(
                {
                    "connect_args": {
                        "check_same_thread": False,
                        "timeout": 30,
                    },
                    "poolclass": StaticPool,
                }
            )
        self.engine = create_engine(url, **engine_options)
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        from ops_agent import models  # noqa: F401

        Base.metadata.create_all(self.engine)
