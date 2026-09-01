"""
Database connection setup.
V1 uses SQLite (file-based, zero-config) so the app runs immediately
without needing a separate database server. On Render, this file lives
on the container's disk. For a permanent production DB later, swap
DATABASE_URL for a PostgreSQL/Supabase connection string - the rest
of the code does not need to change.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stock_analyzer.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
