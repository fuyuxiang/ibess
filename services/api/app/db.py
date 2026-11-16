from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_timeout=5,
                       connect_args={'connect_timeout': 3} if settings.database_url.startswith('postgresql') else {})
SessionLocal = sessionmaker(bind=engine)


def get_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
