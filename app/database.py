
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from app.models import Base

# Load environment variables
load_dotenv()

# Construct the database URL safely
database_url = URL.create(
    drivername="postgresql+psycopg",
    username=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    database=os.getenv("POSTGRES_DB"),
)

# Create SQLAlchemy engine
engine = create_engine(database_url, hide_parameters=True)


def create_tables():
    """Create database tables based on the defined models."""
    Base.metadata.create_all(engine)
