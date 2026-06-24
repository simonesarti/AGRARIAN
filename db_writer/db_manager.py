import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from constants import (
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_QUEUE_SIZE,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
)

logger = logging.getLogger("db_writer.manager")

"""
DATABASE SCHEMA

User 1 ────<N Flight 1 ────<N Alert

------
users
------
user_id (PK)
email
password
created_at

-------
flights
-------
flight_id (PK)
user_id (FK → users.user_id)
start_time
stream_url  # Video stream URL, set once the video writer starts

------
alerts
------
alert_id (PK)
flight_id (FK → flights.flight_id)
alert_msg
frame_id
timestamp
datetime
image_data  # Compressed JPEG
image_width
image_height
"""

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True)
    password = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    flights = relationship("Flight", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.user_id}, email='{self.email}')>"

    @staticmethod
    def hash_password(plaintext_password: str) -> str:
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(plaintext_password.encode('utf-8'), salt)
        return hashed.decode('utf-8')

    def verify_password(self, plaintext_password: str) -> bool:
        return bcrypt.checkpw(
            plaintext_password.encode('utf-8'),
            self.password.encode('utf-8')
        )


class Flight(Base):
    __tablename__ = 'flights'

    flight_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    start_time = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    stream_url = Column(String, nullable=True)

    user = relationship("User", back_populates="flights")
    alerts = relationship("Alert", back_populates="flight", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Flight(id={self.flight_id}, user_id={self.user_id}, start='{self.start_time}')>"


class Alert(Base):
    __tablename__ = 'alerts'

    alert_id = Column(Integer, primary_key=True, autoincrement=True)
    flight_id = Column(Integer, ForeignKey('flights.flight_id'), nullable=False)
    alert_msg = Column(String, nullable=False)
    frame_id = Column(Integer)
    timestamp = Column(Float)
    datetime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    image_data = Column(LargeBinary)
    image_width = Column(Integer)
    image_height = Column(Integer)

    flight = relationship("Flight", back_populates="alerts")

    def __repr__(self):
        return (f"<Alert(id={self.alert_id}, flight_id={self.flight_id}, "
                f"msg='{self.alert_msg}...', frame={self.frame_id})>")


class DatabaseManager:
    """Manages database operations for alert storage."""

    def __init__(
            self,
            database_url: str,
            alerts_queue_size: int = DB_MANAGER_QUEUE_SIZE,
            pool_size: int = DB_MANAGER_POOL_SIZE,
            max_overflow: int = DB_MANAGER_MAX_OVERFLOW,
            queue_get_timeout: float = DB_MANAGER_QUEUE_WAIT_TIMEOUT,
            thread_close_timeout: float = DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    ):
        self.database_url = database_url
        self._db_engine = None
        self._db_session: Optional[Session] = None

        self.pool_size = pool_size
        self.max_overflow = max_overflow

        self._db_queue = queue.Queue(maxsize=alerts_queue_size)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._queue_get_timeout = queue_get_timeout
        self._thread_close_timeout = thread_close_timeout

        self.flight_id = None

    def initialize(self, username: str, password: str):
        """
        Verify credentials, create tables if needed, open a new Flight record,
        and start the background writer thread.
        Raises ValueError on bad credentials, Exception on DB failure.
        """
        logger.info(f"Initializing database connection: {self.database_url}")
        self._db_engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_size=self.pool_size,
            connect_args={'connect_timeout': 5},
            max_overflow=self.max_overflow,
            echo=False,
        )
        Base.metadata.create_all(self._db_engine)

        SessionFactory = sessionmaker(bind=self._db_engine)
        with SessionFactory() as session:
            try:
                user = session.query(User).filter_by(email=username).first()
                if not user or not user.verify_password(password):
                    raise ValueError("Authentication failed: Invalid credentials.")
                logger.info("User credentials approved")

                new_flight = Flight(user_id=user.user_id)
                session.add(new_flight)
                session.commit()
                self.flight_id = new_flight.flight_id
                logger.info("New flight record created successfully")

            except Exception as e:
                session.rollback()
                raise e

        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._db_worker, daemon=True)
        self._worker_thread.start()
        logger.info("Database manager and worker thread initialized")

    def set_stream_url(self, url: str) -> bool:
        if self._db_engine is None:
            return False
        SessionFactory = sessionmaker(bind=self._db_engine)
        try:
            with SessionFactory() as session:
                flight = session.get(Flight, self.flight_id)
                if flight is None:
                    logger.error(f"Flight {self.flight_id} not found; cannot set stream URL.")
                    return False
                flight.stream_url = url
                session.commit()
                logger.info(f"Flight {self.flight_id} stream URL set to: {url}")
                return True
        except Exception as e:
            logger.error(f"Failed to set stream URL for flight {self.flight_id}: {e}")
            return False

    def save_alert(self, **kwargs) -> bool:
        if self._db_engine is None:
            return False
        try:
            kwargs["flight_id"] = self.flight_id
            self._db_queue.put_nowait(kwargs)
            logger.debug(f"Alert queued: frame={kwargs.get('frame_id')}, msg={kwargs.get('alert_msg')}")
            return True
        except queue.Full:
            logger.warning(
                f"DB queue full — dropping alert for frame {kwargs.get('frame_id')}. "
                "Consider increasing DB_MANAGER_QUEUE_SIZE."
            )
            return False

    def close(self):
        self._stop_event.set()
        if self._worker_thread:
            try:
                self._worker_thread.join(timeout=self._thread_close_timeout)
                logger.info("DB manager thread terminated successfully")
            except Exception as e:
                logger.error(f"Failed to terminate DB worker thread: {e}")
        if self._db_engine:
            try:
                self._db_engine.dispose()
                logger.info("Database engine disposed")
            except Exception as e:
                logger.error(f"Error disposing database engine: {e}")

    def _db_worker(self):
        logger.info("Database background worker started")
        SessionFactory = sessionmaker(bind=self._db_engine)

        while not self._stop_event.is_set() or not self._db_queue.empty():
            try:
                alert_params = self._db_queue.get(timeout=self._queue_get_timeout)
            except queue.Empty:
                continue

            try:
                with SessionFactory() as session:
                    db_alert = Alert(**alert_params)
                    session.add(db_alert)
                    session.commit()
                    logger.info(f"Committed alert: frame={alert_params['frame_id']}, msg={alert_params['alert_msg']}")
            except Exception as e:
                logger.error(f"DB worker error for frame {alert_params.get('frame_id')}: {e}")
            finally:
                self._db_queue.task_done()

        logger.info("Database background worker finished")
