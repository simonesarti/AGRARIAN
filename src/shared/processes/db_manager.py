from typing import Optional
import logging
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, LargeBinary, Float
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
import queue
import threading
from src.shared.processes.constants import *
from datetime import datetime, timezone
import bcrypt


# ================================================================

logger = logging.getLogger("main.alert_out.db")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/alert_out_db.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


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
    password = Column(String(128), nullable=False)  # HASHED PASSWORD
    created_at = Column(DateTime, default=datetime.now(timezone.utc))

    # Relationship: One User can have many Flights
    flights = relationship("Flight", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.user_id}, email='{self.email}')>"

    @staticmethod
    def hash_password(plaintext_password: str) -> str:
        """Converts a password to a secure hash for storage."""
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(plaintext_password.encode('utf-8'), salt)
        return hashed.decode('utf-8')

    def verify_password(self, plaintext_password: str) -> bool:
        """Checks if the provided password matches the stored hash."""
        return bcrypt.checkpw(
            plaintext_password.encode('utf-8'),
            self.password.encode('utf-8')
        )


class Flight(Base):
    __tablename__ = 'flights'

    flight_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    start_time = Column(DateTime, default=datetime.now(timezone.utc))
    stream_url = Column(String, nullable=True)  # Set once the video writer starts

    # Relationships: One Flight is associated to a single User
    user = relationship("User", back_populates="flights")
    # Relationship: One Flight can have many Alerts
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
    datetime = Column(DateTime, default=datetime.now(timezone.utc))

    # Image Data
    image_data = Column(LargeBinary)  # Stores compressed JPEG bytes
    image_width = Column(Integer)
    image_height = Column(Integer)

    # Relationship: one Alerts is associated with a single Flight
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
        """
        Initialize the database manager.

        Args:
            database_url: SQLAlchemy database URL
        """
        self.database_url = database_url        # always set, otherwise DB_manager not created
        self._db_engine = None
        self._db_session: Optional[Session] = None

        self.pool_size = pool_size
        self.max_overflow = max_overflow

        # Background worker components
        self._db_queue = queue.Queue(maxsize=alerts_queue_size)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._queue_get_timeout = queue_get_timeout
        self._thread_close_timeout = thread_close_timeout

        # prepare field for a new flight
        self.flight_id = None

    def initialize(self, username: str, password: str):
        """
        Initialize database connection and create tables if they don't exist.
        raises Exception on failure (try/except skipped).
        Also creates a new Flight entry for the current session.
        """

        logger.info(f"Initializing database connection: {self.database_url}")
        self._db_engine = create_engine(
            self.database_url,
            pool_pre_ping=True,                     # Checks if connection is alive before using it
            pool_size=self.pool_size,               # Number of permanent connections
            connect_args={'connect_timeout': 5},    # 5 second limit to connect
            max_overflow=self.max_overflow,         # Allow extra connections during spikes
            echo=False,
        )
        Base.metadata.create_all(self._db_engine)

        SessionFactory = sessionmaker(bind=self._db_engine)
        with SessionFactory() as session:
            try:
                # 1. Fetch user by email
                user = session.query(User).filter_by(email=username).first()
                
                #if user:
                #    print(f"DEBUG: Provided password: {password}")
                #    print(f"DEBUG: Stored hash in DB: {user.password}")
                #    # Check the exact length: a standard bcrypt hash is exactly 60 characters
                #    print(f"DEBUG: Hash length: {len(user.password)}") 
                #    print(f"DEBUG: Hash repr: {repr(user.password)}") # This will show hidden \n or \r
                #    # Manually try a check
                #    is_match = bcrypt.checkpw(password.encode('utf-8'), user.password.encode('utf-8'))
                #    print(f"DEBUG: Manual bcrypt check match: {is_match}")

                # 2. check that the user exists and that the password matches
                if not user or not user.verify_password(password):
                    err_msg = "Authentication failed: Invalid credentials."
                    logger.error(err_msg)
                    raise ValueError(err_msg)
                
                logger.info("User credentials approved")

                # 3. Success - Create Flight
                new_flight = Flight(user_id=user.user_id)
                session.add(new_flight)
                session.commit()
                self.flight_id = new_flight.flight_id
                logger.info("New flight record created succesfully")
            
            except Exception as e:
                session.rollback() # Undo changes if something crashes
                err_msg = "unexpected failure during credentials check or flight record creation. Rolling back."
                logger.error(err_msg)
                raise e

        # Start the background worker thread
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._db_worker, daemon=True)
        self._worker_thread.start()
        logger.info("Database manager and worker thread initialized")

    def set_stream_url(self, url: str) -> bool:
        """
        Update the stream_url field on the current flight record.
        Called once the video writer has started and the URL is known.
        Returns True on success, False if DB is not initialised or the update fails.
        """
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
        """
        Asynchronously queue an alert to be saved to the database.
        Returns True if queued, False if DB is disabled.
        """
        if self._db_engine is None:
            return False

        # Drop the data into the queue and return immediately
        try:
            kwargs["flight_id"] = self.flight_id    # add flight_id to alert parameters
            self._db_queue.put_nowait(kwargs)
            logger.debug(
                "Alert saved in queue for database write: "
                f"Frame id: {kwargs.get('frame_id')}, "
                f"msg: {kwargs.get('alert_msg')}"
            )
            return True

        except queue.Full:
            logger.warning(
                f"Database queue is full (maxsize reached). "
                f"Dropping alert for Frame id: {kwargs.get('frame_id')}. "
                "Consider increasing DB_MANAGER_QUEUE_MAX_SIZE or optimizing DB performance."
            )
            return False

    def close(self):
        """Signal the worker to finish and close connections."""
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
        """Background worker that handles the actual SQL I/O."""

        logger.info("Database background worker started")

        # Sessions are NOT thread-safe, so we create it inside the worker thread
        SessionFactory = sessionmaker(bind=self._db_engine)

        # continue to write alerts until the queue has not been emptied
        # even if the closing signal has been set
        # The thread may be killed if timeout is exceeded
        while not self._stop_event.is_set() or not self._db_queue.empty():

            # Wait for an alert with a timeout to check the stop_event
            try:
                alert_params = self._db_queue.get(timeout=self._queue_get_timeout)
            except queue.Empty:
                logger.debug("Alert queue empty. Continuing to wait for alert to appear ... ")
                continue

            # try to commit the alert to the DB
            # This outer try protects the thread from connection failures
            try:
                # 'with' clause handles __enter__ (connect), __exit__ (close), and automatic rollback on error.
                with SessionFactory() as session:
                    db_alert = Alert(**alert_params)
                    session.add(db_alert)
                    session.commit()
                    logger.info(
                        f"Committed a DB alert entry. "
                        f"Frame id: {alert_params['frame_id']}, "
                        f"msg: {alert_params['alert_msg']}"
                    )
            except Exception as e:
                # session.rollback() is handled automatically by the 'with' block if the error happened inside it.
                logger.error(f"DB Worker error for frame {alert_params.get('frame_id')}: {e}")
            finally:
                # Always signal task_done to allow the queue to drain properly
                self._db_queue.task_done()

        logger.info("Database background worker finished")





if __name__ == "__main__":

    from time import time,sleep, perf_counter
    import random
    import datetime as dtt
    import numpy as np
    import cv2


    VSLOW = 1
    SLOW = 10
    FAST = 50
    REAL = 30
    FREAL = 40

    speed = REAL

    QUEUE_MAX = 3

    N_ALERTS = 100


    # db_url="sqlite:///alerts.db"  # SQLite
    db_url="postgresql://app_manager:app_manager_pass@localhost:5432/agrarian_db"    # PostgreSQL
    # db_url="mysql+pymysql://app_manager:app_manager_pass@localhost:3306/agrarian_db"    # MySQL

    email="testuser@testmail.com"
    plaintext_password="testpassword"

    def create_test_db(database_url, email="testuser@testmail.com", plaintext_password="testpassword"):
    
        db_engine = create_engine(database_url, echo=True)
        Base.metadata.create_all(db_engine)
        SessionFactory = sessionmaker(bind=db_engine)
        with SessionFactory() as session:
            # Check if exists
            exists = session.query(User).filter_by(email=email).first()
            if not exists:
                hashed = User.hash_password(plaintext_password)
                test_user = User(email=email, password=hashed)
                session.add(test_user)
                session.commit()
                print(f"Test user {email} created with secure hash.")

    def generate_db_alert_object(frame_id:int):
        ts = time()
        # Encode as JPEG
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        _, buffer = cv2.imencode('.jpg', frame, encode_param)
        compressed_bytes = buffer.tobytes()
        return {
                "frame_id": frame_id,
                "alert_msg": random.choice(["road", "car", "slope"]),
                "timestamp": ts,
                "datetime": dtt.datetime.fromtimestamp(ts),
                "image_data": compressed_bytes,
                "image_width": 1920,
                "image_height": 1080,
        }
    

    # create_test_db(db_url, email, plaintext_password)

    db_manager = DatabaseManager(db_url)
    # db_manager.initialize(email, plaintext_password+"wrong")   # should fail
    # db_manager.initialize(email+"wrong", plaintext_password)   # should fail
    db_manager.initialize(email, plaintext_password)           # should pass

    next = perf_counter() + 1/speed

    for i in range(N_ALERTS):

        alert = generate_db_alert_object(i)
        db_manager.save_alert(**alert)

        perf = perf_counter()
        if perf < next:
            sleep(next-perf)
        next += 1/speed

    db_manager.close()