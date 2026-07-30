import logging
import queue
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import bcrypt
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from constants import (
    ALERT_QUEUE_SIZE,
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    STREAM_KEY_ALPHABET,
    STREAM_KEY_LENGTH,
)

logger = logging.getLogger("db_writer.manager")

"""
DATABASE SCHEMA

User 1 ────<N Stream 1 ────<N Flight 1 ────<N Alert

A "stream" is a concurrency slot, not an aircraft: it is one ingest credential the
user can publish on. Users add streams according to how many feeds they need to run
at the same time. Nothing here models a physical drone, and no aircraft is tracked
across owners.

Strictly linear. A flight belongs to a stream, and the owning user is reached through
it — flights carry no user_id of their own, because a redundant one could contradict
streams.user_id and there would be no way to tell which was right.

A flight is the tenancy unit of the whole system: it scopes alert rows, WebSocket
delivery, Redis channels and the annotated output path. Two feeds running at once are
two streams and therefore two independent flights.

------
users
------
user_id (PK)
email
password
created_at

-------
streams
-------
stream_id (PK)
user_id (FK → users.user_id)
stream_key   # Typed into the controller by hand; doubles as the ingest path
label        # Operator-facing name ("north field quad")
revoked_at   # Non-null retires the slot without destroying flight history
created_at

-------
flights
-------
flight_id (PK)
stream_id (FK → streams.stream_id)   # Owner reached via streams.user_id
public_uuid  # Random; names the annotated output path (out/<public_uuid>)
start_time
output_url   # Annotated output URL, set once the video writer starts

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


def generate_stream_key() -> str:
    """
    Mint an ingest key for a stream.

    secrets.choice rather than random.choice — these are credentials, and the
    default RNG is predictable from prior outputs.
    """
    return "".join(secrets.choice(STREAM_KEY_ALPHABET) for _ in range(STREAM_KEY_LENGTH))

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True)
    password = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # No direct flights relationship — they hang off streams. Deleting a user cascades
    # users → streams → flights → alerts down the chain.
    streams = relationship("Stream", back_populates="user", cascade="all, delete-orphan")

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


class Stream(Base):
    """
    One concurrency slot belonging to a user, holding one persistent ingest credential.

    Deliberately NOT a drone. Nothing here identifies a physical aircraft, and none is
    tracked across owners — a drone sold to someone else is simply that user adding a
    stream of their own, with a key unrelated to this one.

    Streams exist because a key doubles as the ingest path: one key means one path, so
    a user who needs two simultaneous feeds needs two streams. Users add slots as their
    concurrency needs grow and retire them when they shrink. Granular revocation falls
    out of that for free — retiring one slot leaves the user's others publishing.
    """

    __tablename__ = 'streams'

    stream_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    # Indexed and unique: every MediaMTX connection attempt resolves a key, so this
    # lookup sits on the hot path for both publishing and viewing.
    stream_key = Column(String(64), nullable=False, unique=True, index=True,
                        default=generate_stream_key)
    label = Column(String(128), nullable=True)
    # Revocation is a timestamp, not a delete: flights reference the stream, and the
    # record of what was published when has to survive both rotation and retirement.
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="streams")
    # This cascade exists for ONE case: deleting a user account, which must erase
    # everything below it. It is NOT the per-stream "remove" operation — deleting a
    # stream row would silently destroy its flight history and every alert with it.
    # Removing a stream means revoke_stream(); there is deliberately no delete_stream().
    flights = relationship("Flight", back_populates="stream", cascade="all, delete-orphan")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __repr__(self):
        state = "active" if self.is_active else "revoked"
        return f"<Stream(id={self.stream_id}, user_id={self.user_id}, label='{self.label}', {state})>"


class Flight(Base):
    __tablename__ = 'flights'

    flight_id = Column(Integer, primary_key=True, autoincrement=True)
    # The only ownership link. A flight exists because a publisher connected on a
    # valid stream key, so there is always a stream; the user is streams.user_id.
    stream_id = Column(Integer, ForeignKey('streams.stream_id'), nullable=False, index=True)
    # Names the annotated output path (out/<public_uuid>). Deliberately NOT derived
    # from flight_id: the sequential PK would make every tenant's video path
    # enumerable, and read authorisation is the only thing in front of it.
    public_uuid = Column(String(36), nullable=False, unique=True, index=True,
                         default=lambda: str(uuid.uuid4()))
    start_time = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Set when the publisher disconnects and the orchestrator tears the flight down.
    # NULL means "still in the air" — which is also what it means for a flight whose
    # orchestrator died before it could close, so this is not a liveness signal.
    end_time = Column(DateTime, nullable=True)
    # The ANNOTATED OUTPUT URL, never the ingest one. Ingest URLs embed the stream key,
    # so storing one here would scatter live credentials through flight history and
    # leave dead ones behind after every rotation.
    output_url = Column(String, nullable=True)

    stream = relationship("Stream", back_populates="flights")
    alerts = relationship("Alert", back_populates="flight", cascade="all, delete-orphan")

    @property
    def user_id(self) -> int:
        """Owner, via the stream. Read-only — no user_id column exists to disagree with."""
        return self.stream.user_id

    def __repr__(self):
        return (f"<Flight(id={self.flight_id}, stream_id={self.stream_id}, "
                f"start='{self.start_time}')>")


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


class UserDirectory:
    """
    Lookups and stream management against users/streams/flights.

    Holds no per-flight state, so every method works on any replica. This serves the
    UI, the MediaMTX auth hook, and flight creation.
    """

    def __init__(self, database_url: str):
        # create_engine is lazy — no connection opens until the first query, so
        # constructing this at import time cannot delay or fail startup.
        self._engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
            connect_args={'connect_timeout': 5},
        )

    def authenticate(self, email: str, password: str) -> int:
        """Return the user_id for valid credentials; raise ValueError otherwise."""
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            user = session.query(User).filter_by(email=email).first()
            if not user or not user.verify_password(password):
                raise ValueError("Authentication failed: Invalid credentials.")
            return user.user_id

    # ── Stream management ─────────────────────────────────────────────────────

    def resolve_stream_key(self, stream_key: str) -> Optional[dict]:
        """
        Resolve an ingest key to its stream and owner, or None if unknown/revoked.

        This is the hot path — MediaMTX calls it on every connection attempt — and
        it is also the authorisation decision itself, so it deliberately returns
        nothing at all for a revoked key rather than reporting why.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            stream = session.query(Stream).filter_by(stream_key=stream_key).first()
            if stream is None or stream.revoked_at is not None:
                return None
            return {"stream_id": stream.stream_id, "user_id": stream.user_id, "label": stream.label}

    def create_stream(self, user_id: int, label: Optional[str] = None) -> dict:
        """Add a stream slot and mint its ingest key. The key is returned once, here."""
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            stream = Stream(user_id=user_id, label=label, stream_key=generate_stream_key())
            session.add(stream)
            session.commit()
            logger.info(f"Stream added: stream_id={stream.stream_id}, user_id={user_id}")
            return {"stream_id": stream.stream_id, "stream_key": stream.stream_key, "label": stream.label}

    def list_streams(self, user_id: int, include_revoked: bool = False) -> List[dict]:
        """
        Streams belonging to this user.

        Retired ones are hidden by default, which is what makes "remove from my
        portal" a display concern rather than a delete: the row and its flight
        history stay, the user simply stops seeing it.

        Stream keys are included: unlike a password these are recoverable by design,
        because the operator has to retype the ingest URL before every flight.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            query = session.query(Stream).filter_by(user_id=user_id)
            if not include_revoked:
                query = query.filter(Stream.revoked_at.is_(None))
            streams = query.order_by(Stream.stream_id).all()
            return [
                {
                    "stream_id": d.stream_id,
                    "label": d.label,
                    "stream_key": d.stream_key,
                    "revoked_at": d.revoked_at,
                    "created_at": d.created_at,
                }
                for d in streams
            ]

    def revoke_stream(self, stream_id: int, user_id: int) -> None:
        """
        Retire a stream slot, killing its key. This is what "remove" means in the
        portal — no row is deleted and no flight or alert is touched.

        user_id is required and checked; without it any caller could retire any
        stream by guessing a sequential id.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            stream = session.query(Stream).filter_by(stream_id=stream_id, user_id=user_id).first()
            if stream is None:
                raise ValueError(f"Stream {stream_id} not found for this user")
            stream.revoked_at = datetime.now(timezone.utc)
            session.commit()
            logger.info(f"Stream retired: stream_id={stream_id}, user_id={user_id}")

    def rotate_stream_key(self, stream_id: int, user_id: int) -> str:
        """
        Issue a new key for a registration, invalidating the old one immediately.

        This is the response to a leaked key that keeps the registration usable, as
        opposed to revoking it outright. Any flight currently in the air keeps
        streaming — MediaMTX only re-checks on connect — so it takes effect from the
        next flight.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            stream = session.query(Stream).filter_by(stream_id=stream_id, user_id=user_id).first()
            if stream is None:
                raise ValueError(f"Stream {stream_id} not found for this user")
            stream.stream_key = generate_stream_key()
            stream.revoked_at = None
            session.commit()
            logger.info(f"Stream key rotated: stream_id={stream_id}, user_id={user_id}")
            return stream.stream_key

    # ── Flights ───────────────────────────────────────────────────────────────

    def start_flight(self, email: str, password: str,
                     stream_id: Optional[int] = None) -> dict:
        """
        Authenticate the user and open a flight, returning its identifiers.

        Deliberately returns plain data and keeps no handle: the flight lives in the
        database, not in this process. That is what lets a later alert for the same
        flight be served by a different db-writer replica.

        Raises ValueError on bad credentials or an unusable stream.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            user = session.query(User).filter_by(email=email).first()
            if not user or not user.verify_password(password):
                raise ValueError("Authentication failed: Invalid credentials.")

            if stream_id is None:
                # Interim path: the app opens flights with user credentials and has no
                # stream in hand. The orchestrator always knows the stream, because the
                # stream key is what identified the flight, and this branch then goes.
                active = [st for st in user.streams if st.revoked_at is None]
                if len(active) != 1:
                    raise ValueError(
                        f"Cannot infer stream: user has {len(active)} active streams. "
                        "Pass stream_id explicitly."
                    )
                stream = active[0]
            else:
                # Never let a caller attach a flight to someone else's stream.
                stream = session.query(Stream).filter_by(
                    stream_id=stream_id, user_id=user.user_id).first()
                if stream is None:
                    raise ValueError(f"Stream {stream_id} does not belong to this user")
                if stream.revoked_at is not None:
                    raise ValueError(f"Stream {stream_id} is retired")

            flight = Flight(stream_id=stream.stream_id)
            session.add(flight)
            session.commit()

            logger.info(
                f"Flight opened: flight_id={flight.flight_id}, "
                f"stream_id={stream.stream_id}, user_id={user.user_id}"
            )
            return {
                "flight_id": flight.flight_id,
                "public_uuid": flight.public_uuid,
                "stream_id": stream.stream_id,
                "user_id": user.user_id,
            }

    def open_flight_for_key(self, stream_key: str) -> Optional[dict]:
        """
        Open a flight on the stream this key identifies, or None if it is unusable.

        The orchestrator's entry point, and the reason the app no longer needs end-user
        credentials: a publisher proved possession of the key to MediaMTX, so the key —
        not an email and password carried inside a GPU container — is what opens the
        flight.

        Returns None rather than raising for an unknown or revoked key: MediaMTX has
        already accepted the publisher by this point, so this is a race (revoked between
        connect and go-live), not an error worth a stack trace.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            stream = session.query(Stream).filter_by(stream_key=stream_key).first()
            if stream is None or stream.revoked_at is not None:
                return None

            flight = Flight(stream_id=stream.stream_id)
            session.add(flight)
            session.commit()

            logger.info(
                f"Flight opened by key: flight_id={flight.flight_id}, "
                f"stream_id={stream.stream_id}, user_id={stream.user_id}"
            )
            return {
                "flight_id": flight.flight_id,
                "public_uuid": flight.public_uuid,
                "stream_id": stream.stream_id,
                "user_id": stream.user_id,
            }

    def close_flight(self, flight_id: int) -> bool:
        """
        Stamp a flight as finished. False if there is no such flight.

        Idempotent, and deliberately does not overwrite an existing end_time: a stream
        that drops and reconnects can deliver a late teardown for a flight that already
        closed, and the first timestamp is the true one.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = session.get(Flight, flight_id)
            if flight is None:
                return False
            if flight.end_time is None:
                flight.end_time = datetime.now(timezone.utc)
                session.commit()
                logger.info(f"Flight closed: flight_id={flight_id}")
            return True

    def set_output_url(self, flight_id: int, url: str) -> bool:
        """
        Record where the annotated output went. Never pass an ingest URL here —
        those embed the stream key, which must not land in flight history.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = session.get(Flight, flight_id)
            if flight is None:
                logger.error(f"Flight {flight_id} not found; cannot set output URL.")
                return False
            flight.output_url = url
            session.commit()
            logger.info(f"Flight {flight_id} output URL set to: {url}")
            return True

    def resolve_public_uuid(self, public_uuid: str) -> Optional[int]:
        """
        Resolve an output path's UUID to its flight_id, or None if unknown.

        This is the read side of the MediaMTX auth hook: viewers are handed a token
        naming a flight_id, but the path they open names a public_uuid, so one of the
        two has to be translated before they can be compared.

        Like resolve_stream_key this is the authorisation decision itself, so an
        unknown UUID returns nothing rather than reporting why.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = session.query(Flight).filter_by(public_uuid=public_uuid).first()
            return flight.flight_id if flight else None

    def flight_stream_id(self, flight_id: int) -> Optional[int]:
        """
        The stream a flight was opened on, or None if the flight is unknown.

        Used to authorise an app container reading the ingest path: its token names a
        flight, the path names a stream key, and the two must belong together — a
        publisher token for flight 7 must not open the raw feed of somebody else's
        drone just because flight 7 happens to be in the air.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = session.get(Flight, flight_id)
            return flight.stream_id if flight else None

    def latest_flight_id(self, user_id: int) -> Optional[int]:
        """
        Most recently started flight for this user, or None if they have none.

        Joins through streams — a flight's owner is streams.user_id. Ambiguous once a
        user has two streams running at once; see the note in CLOUD_ARCHITECTURE §9.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = (
                session.query(Flight)
                .join(Stream, Flight.stream_id == Stream.stream_id)
                .filter(Stream.user_id == user_id)
                .order_by(Flight.start_time.desc())
                .first()
            )
            return flight.flight_id if flight else None


class AlertWriter:
    """
    Process-wide alert writer: ONE per db-writer instance, not one per flight.

    This is what makes db-writer replicable. The previous design kept a manager per
    flight in a module-level dict, so a replica could only serve flights whose
    /session/start it had personally handled — a second replica returned 404 for
    every alert belonging to a flight opened elsewhere. Nothing here is keyed by
    flight: the flight_id rides on each queued item, so any replica can accept any
    alert for any flight.

    The queue is kept because it decouples the caller from database latency. The app
    POSTs an alert from its inference pipeline and must not block on a slow commit,
    so enqueue() returns as soon as the item is accepted and a background thread does
    the writing. Alerts are best-effort: a full queue drops rather than blocks, which
    is the right trade when the alternative is stalling frame processing.
    """

    def __init__(
            self,
            database_url: str,
            queue_size: int = ALERT_QUEUE_SIZE,
            pool_size: int = DB_MANAGER_POOL_SIZE,
            max_overflow: int = DB_MANAGER_MAX_OVERFLOW,
            queue_get_timeout: float = DB_MANAGER_QUEUE_WAIT_TIMEOUT,
            thread_close_timeout: float = DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    ):
        self._engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={'connect_timeout': 5},
            echo=False,
        )
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._queue_get_timeout = queue_get_timeout
        self._thread_close_timeout = thread_close_timeout
        self._dropped = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Create any missing tables and start the writer thread."""
        Base.metadata.create_all(self._engine)
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="alert-writer", daemon=True)
        self._worker_thread.start()
        logger.info("Alert writer started")

    def stop(self) -> None:
        """Drain what is queued, then shut down."""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=self._thread_close_timeout)
            if self._worker_thread.is_alive():
                logger.warning("Alert writer thread did not finish draining in time")
            else:
                logger.info("Alert writer thread terminated")
        self._engine.dispose()
        logger.info("Database engine disposed")

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped

    # ── Write path ────────────────────────────────────────────────────────────

    def enqueue(self, flight_id: int, **fields) -> bool:
        """
        Accept an alert for any flight. Returns False if the queue is full.

        No check that the flight exists: the caller already presented a token this
        service signed for that flight, and the foreign key is the backstop. Querying
        here would put a database round-trip back on the hot path the queue exists to
        keep clear.
        """
        fields["flight_id"] = flight_id
        try:
            self._queue.put_nowait(fields)
            logger.debug(f"Alert queued: flight={flight_id} frame={fields.get('frame_id')}")
            return True
        except queue.Full:
            self._dropped += 1
            logger.warning(
                f"Alert queue full ({self._queue.maxsize}) — dropping alert for "
                f"flight {flight_id} frame {fields.get('frame_id')}. "
                f"{self._dropped} dropped since start; consider raising ALERT_QUEUE_SIZE "
                "or adding replicas."
            )
            return False

    def _worker_loop(self) -> None:
        logger.info("Alert writer thread started")
        SessionFactory = sessionmaker(bind=self._engine)

        # Keep draining after the stop signal so a shutdown does not discard alerts
        # already accepted from the app.
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                params = self._queue.get(timeout=self._queue_get_timeout)
            except queue.Empty:
                continue

            try:
                with SessionFactory() as session:
                    session.add(Alert(**params))
                    session.commit()
                    logger.info(
                        f"Committed alert: flight={params['flight_id']} "
                        f"frame={params.get('frame_id')} msg={params.get('alert_msg')}"
                    )
            except Exception as e:
                # One bad row must not kill the thread — every later alert on this
                # replica would be silently lost with it.
                logger.error(
                    f"Failed to write alert for flight {params.get('flight_id')} "
                    f"frame {params.get('frame_id')}: {e}"
                )
            finally:
                self._queue.task_done()

        logger.info("Alert writer thread finished")
