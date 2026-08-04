import logging
import os
import queue
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import bcrypt
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, create_engine, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from constants import (
    ALERT_QUEUE_SIZE,
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    FLIGHT_ALERTS_PAGE_SIZE,
    FLIGHT_HISTORY_PAGE_SIZE,
    MAX_EMAIL_LENGTH,
    MAX_FLIGHT_HISTORY_PAGE_SIZE,
    MAX_PASSWORD_BYTES,
    MAX_STREAM_LABEL_LENGTH,
    MAX_STREAMS_PER_USER,
    MIN_PASSWORD_LENGTH,
    STREAM_KEY_ALPHABET,
    STREAM_KEY_LENGTH,
)

logger = logging.getLogger("db_writer.manager")

# Per-deployment tunable, same pattern as the token TTLs in auth.py.
_MAX_STREAMS_PER_USER = int(os.environ.get("MAX_STREAMS_PER_USER", MAX_STREAMS_PER_USER))

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
end_time     # Non-null once the orchestrator tore the flight down
output_path  # Media-server path of the annotated output, set when the flight opens

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

----------
recordings
----------
recording_id (PK)
flight_id (FK → flights.flight_id)
segment_path      # Filesystem path MediaMTX wrote, under the shared recordings volume
storage_backend   # local | azure | aws — whichever the recorder was configured with
storage_location  # Blob/key name the recorder uploaded to; NULL for the local backend
uploaded_at
"""


def generate_stream_key() -> str:
    """
    Mint an ingest key for a stream.

    secrets.choice rather than random.choice — these are credentials, and the
    default RNG is predictable from prior outputs.
    """
    return "".join(secrets.choice(STREAM_KEY_ALPHABET) for _ in range(STREAM_KEY_LENGTH))


def normalize_email(email: str) -> str:
    """
    One canonical form per address, applied on every read and every write.

    The unique constraint on users.email is case-SENSITIVE in PostgreSQL, so
    without this "Alice@example.com" and "alice@example.com" are two separate
    accounts — and the user who registers with one and logs in with the other
    gets "invalid credentials" with nothing on screen to explain why.

    Lowercasing the whole address is stricter than RFC 5321, which makes the
    local part case-sensitive on paper. No mail provider treats it that way in
    practice, and the alternative is a class of unexplainable login failures.

    Applied in exactly one place per direction — create_user and authenticate —
    which is the only way it protects anything: normalising on write alone would
    still fail to match a differently-cased login.
    """
    return email.strip().lower()


class EmailAlreadyRegistered(ValueError):
    """
    Raised by create_user when the address is taken.

    A ValueError subclass on purpose: callers that catch ValueError for the other
    validation failures keep working unchanged, while the HTTP layer can still
    separate "this email exists" (409) from "this password is too short" (400)
    without matching on message text.
    """


class StreamLimitReached(ValueError):
    """
    Raised by create_stream when the user already holds the maximum active slots.

    A ValueError subclass for the same reason as EmailAlreadyRegistered: the HTTP
    layer answers 409 for "your account is full" and 400 for "that label is too
    long" without reading the message.
    """


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
    # Media-server PATH of the annotated output — `out/<public_uuid>` — and never the
    # ingest path, which embeds the stream key and would scatter live credentials
    # through flight history, leaving dead ones behind after every rotation.
    #
    # A path rather than a full URL, and not only for brevity. The app's own output URL
    # carries its publisher token in the query string, so storing what the app
    # publishes to would write a live credential into a row the portal reads. And the
    # host a viewer should dial is not the host the app publishes to: the portal
    # composes the viewer URL from this path and the public media hostname it knows.
    output_path = Column(String, nullable=True)

    stream = relationship("Stream", back_populates="flights")
    alerts = relationship("Alert", back_populates="flight", cascade="all, delete-orphan")
    recordings = relationship("Recording", back_populates="flight", cascade="all, delete-orphan")

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


class Recording(Base):
    """
    One uploaded recording segment, tying it back to the flight it came from.

    MediaMTX names segments by output path (out/<public_uuid>), not flight_id, so this
    is the only place that association is ever recorded. Without it, "give me flight
    42's recordings" has no answer beyond knowing its public_uuid and listing a storage
    prefix by hand.

    A flight can own more than one row: recordSegmentDuration splits long sessions into
    hourly files, and each one completes and uploads independently.
    """

    __tablename__ = 'recordings'

    recording_id = Column(Integer, primary_key=True, autoincrement=True)
    flight_id = Column(Integer, ForeignKey('flights.flight_id'), nullable=False, index=True)
    segment_path = Column(String, nullable=False)
    storage_backend = Column(String(16), nullable=False)
    storage_location = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    flight = relationship("Flight", back_populates="recordings")

    def __repr__(self):
        return (f"<Recording(id={self.recording_id}, flight_id={self.flight_id}, "
                f"backend='{self.storage_backend}')>")


def ingest_path(stream_key: str) -> str:
    """The media-server path a drone publishes to. The key IS the credential here."""
    return f"in/{stream_key}"


def output_path_for(public_uuid: str) -> str:
    """The media-server path the app publishes the annotated video to."""
    return f"out/{public_uuid}"


class UserDirectory:
    """
    Lookups and stream management against users/streams/flights.

    Holds no per-flight state, so every method works on any replica. 
    This serves the UI, the MediaMTX auth hook, and flight creation.
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

    # ── Accounts ──────────────────────────────────────────────────────────────

    def create_user(self, email: str, password: str) -> dict:
        """
        Register an account. Returns {"user_id", "email"} with the stored form of
        the address.

        Registration is open, so this is reachable by anyone on the internet and
        both arguments are untrusted. It is also the only method here that creates
        a principal rather than something belonging to one.

        Raises EmailAlreadyRegistered if the address is taken, ValueError for any
        other rejection.
        """
        email = normalize_email(email)

        if not email:
            raise ValueError("Email is required")
        if len(email) > MAX_EMAIL_LENGTH:
            raise ValueError(f"Email must be at most {MAX_EMAIL_LENGTH} characters")
        # Deliberately not RFC-complete: a full grammar is large, and every address
        # it would additionally reject is one that simply fails to receive mail.
        # This catches the input that is not an address at all.
        local, sep, domain = email.partition("@")
        if not sep or not local or not domain or "." not in domain:
            raise ValueError("Email is not a valid address")
        # Interior whitespace survives .strip() and would otherwise pass every check
        # above — "a b@c.d" has a local part, a domain and a dot.
        if any(c.isspace() for c in email):
            raise ValueError("Email is not a valid address")

        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        # Checked in bytes because that is the unit bcrypt's limit is expressed in,
        # and it is measured BEFORE hashing so an over-long passphrase is a clear
        # rejection rather than the ValueError bcrypt itself would raise from inside
        # hash_password, which the caller could only report as a 500.
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes")

        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            user = User(email=email, password=User.hash_password(password))
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                # Insert first and let the unique constraint decide, rather than
                # SELECT-then-INSERT. db-writer runs N replicas (§4), so a check
                # before the insert is a race two simultaneous registrations of the
                # same address can both pass — the constraint is the only arbiter
                # that sees both.
                session.rollback()
                raise EmailAlreadyRegistered("That email is already registered")

            logger.info(f"User registered: user_id={user.user_id}")
            return {"user_id": user.user_id, "email": user.email}

    def authenticate(self, email: str, password: str) -> int:
        """Return the user_id for valid credentials; raise ValueError otherwise."""
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            # Normalised the same way create_user stored it, or a correct password
            # under a differently-cased address would be rejected.
            user = session.query(User).filter_by(email=normalize_email(email)).first()
            if not user or not user.verify_password(password):
                raise ValueError("Authentication failed: Invalid credentials.")
            return user.user_id

    def user_email(self, user_id: int) -> Optional[str]:
        """
        The address on an account, for /me to answer "who am I" with something a
        human recognises. Takes a user_id resolved from a session claim, never one
        supplied by a caller.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            user = session.query(User).filter_by(user_id=user_id).first()
            return user.email if user else None

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
        """
        Add a stream slot and mint its ingest key. The key is returned once, here.

        Capped at MAX_STREAMS_PER_USER *active* slots. This is the endpoint that
        turns an account into GPU capacity, and registration is open, so without a
        cap POST /streams would be unbounded resource creation for anyone who can
        sign up. Retired slots do not count — they cannot publish.

        Raises StreamLimitReached at the cap, ValueError for a bad label.
        """
        if label is not None:
            label = label.strip() or None
            if label is not None and len(label) > MAX_STREAM_LABEL_LENGTH:
                raise ValueError(f"Label must be at most {MAX_STREAM_LABEL_LENGTH} characters")

        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            # Lock the owning user row before counting. Without it two simultaneous
            # requests on two replicas both read "9 active" and both insert, and the
            # cap is advisory. Unlike the duplicate-email case there is no unique
            # constraint to fall back on, so the lock is the only thing making the
            # limit hold. SQLite has no row locks and ignores this, which is fine —
            # the race needs two processes, and SQLite here only ever has one.
            owner = session.query(User).filter_by(user_id=user_id).with_for_update().first()
            if owner is None:
                raise ValueError(f"User {user_id} not found")

            active = (session.query(Stream)
                      .filter_by(user_id=user_id)
                      .filter(Stream.revoked_at.is_(None))
                      .count())
            if active >= _MAX_STREAMS_PER_USER:
                raise StreamLimitReached(
                    f"Already holding the maximum of {_MAX_STREAMS_PER_USER} active streams"
                )

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

        Rotating a RETIRED slot revives it, which is how a user brings one back. That
        makes this a second way to gain an active slot, so it is capped exactly like
        create_stream — otherwise revoke, create, rotate would net one slot over the
        limit every time it was repeated.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            # Same lock as create_stream, for the same reason and against the same
            # counter — the two operations have to serialise against each other, not
            # just against themselves.
            session.query(User).filter_by(user_id=user_id).with_for_update().first()

            stream = session.query(Stream).filter_by(stream_id=stream_id, user_id=user_id).first()
            if stream is None:
                raise ValueError(f"Stream {stream_id} not found for this user")

            if stream.revoked_at is not None:
                active = (session.query(Stream)
                          .filter_by(user_id=user_id)
                          .filter(Stream.revoked_at.is_(None))
                          .count())
                if active >= _MAX_STREAMS_PER_USER:
                    raise StreamLimitReached(
                        f"Reviving this stream would exceed the maximum of "
                        f"{_MAX_STREAMS_PER_USER} active streams"
                    )

            stream.stream_key = generate_stream_key()
            stream.revoked_at = None
            session.commit()
            logger.info(f"Stream key rotated: stream_id={stream_id}, user_id={user_id}")
            return stream.stream_key

    # ── Flights ───────────────────────────────────────────────────────────────

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
            # public_uuid is a column default, so it is not populated until the insert
            # is flushed — the output path cannot be derived before this point.
            session.add(flight)
            session.flush()
            flight.output_path = output_path_for(flight.public_uuid)
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
                # The two paths the app is told to use. Derived here, next to the column
                # that stores one of them, so the naming scheme lives in one place and
                # neither the orchestrator nor the app has to know it.
                "ingest_path": ingest_path(stream_key),
                "output_path": flight.output_path,
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

    def record_upload(
            self,
            public_uuid: str,
            segment_path: str,
            storage_backend: str,
            storage_location: Optional[str] = None,
    ) -> Optional[int]:
        """
        Record that a segment of this flight's output was uploaded. Returns the
        flight_id, or None if public_uuid names no flight.

        Called by the recorder sidecar after a successful upload — it only knows the
        output path MediaMTX gave it, never the flight_id, so this is also where that
        path gets resolved.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            flight = session.query(Flight).filter_by(public_uuid=public_uuid).first()
            if flight is None:
                return None
            recording = Recording(
                flight_id=flight.flight_id,
                segment_path=segment_path,
                storage_backend=storage_backend,
                storage_location=storage_location,
            )
            session.add(recording)
            session.commit()
            logger.info(
                f"Recording logged: flight_id={flight.flight_id}, backend={storage_backend}"
            )
            return flight.flight_id

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

    def active_flights(self, user_id: int) -> list:
        """
        This user's currently open flights (end_time IS NULL) across all their
        streams, most recently started first.

        Joins through streams — a flight's owner is streams.user_id. Used to resolve
        /viewer/token: with one active flight there is nothing to ask; with more than
        one, the caller must say which stream_id it means rather than have one picked
        for it silently. Filtering to open flights also means a flight that already
        landed is never handed out as if it were still live — "latest" alone does not
        imply "active", and the two used to be conflated here.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            rows = (
                session.query(Flight, Stream)
                .join(Stream, Flight.stream_id == Stream.stream_id)
                .filter(Stream.user_id == user_id, Flight.end_time.is_(None))
                .order_by(Flight.start_time.desc())
                .all()
            )
            return [
                {
                    "flight_id": f.flight_id,
                    "stream_id": s.stream_id,
                    "label": s.label,
                    # The portal builds the viewer-facing URL from this plus the
                    # public media hostname, which is why the path is stored rather
                    # than a URL — see the note on Flight.output_path.
                    "output_path": f.output_path,
                    "start_time": f.start_time,
                }
                for f, s in rows
            ]

    # ── Flight history ────────────────────────────────────────────────────────
    #
    # The read side of everything above. Three methods, each joining through
    # streams and filtering on user_id in the same query that selects the row —
    # never fetching first and checking ownership afterwards. That ordering is
    # what makes "not yours" and "does not exist" the same answer, which is the
    # property the portal's 404s rest on.

    def flight_history(
            self,
            user_id: int,
            limit: int = FLIGHT_HISTORY_PAGE_SIZE,
            before: Optional[int] = None,
            stream_id: Optional[int] = None,
    ) -> dict:
        """
        A page of this user's flights, newest first, with alert and recording
        counts. Returns {"flights": [...], "next_before": id or None}.

        **Paged by key, not by offset.** `before` is a flight_id and the page is
        everything below it. OFFSET would be simpler and would be wrong here:
        flights are ordered newest first, so a flight opening while the user
        reads page 1 shifts every later row down by one, and page 2 then repeats
        the row that page 1 ended on. Keyset paging is immune — "older than
        flight 91" means the same thing however many flights start afterwards.

        flight_id descending rather than start_time descending, and they are not
        interchangeable: start_time has no unique constraint, so two flights
        opened in the same clock tick would have an undefined order between them
        and a page boundary landing there could skip one entirely. The PK is
        monotonic and unique, which is what a cursor needs.

        One row more than asked for is fetched, and the extra one is what sets
        next_before. Without it a full last page offers an "older" link that
        leads to nothing, which reads as a bug rather than as the end.
        """
        limit = max(1, min(int(limit), MAX_FLIGHT_HISTORY_PAGE_SIZE))

        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            query = (
                session.query(Flight, Stream)
                .join(Stream, Flight.stream_id == Stream.stream_id)
                .filter(Stream.user_id == user_id)
            )
            if stream_id is not None:
                # Not a security boundary — user_id above already is one. This
                # narrows the page to one slot, and a stream_id belonging to
                # somebody else simply matches nothing.
                query = query.filter(Flight.stream_id == stream_id)
            if before is not None:
                query = query.filter(Flight.flight_id < before)

            rows = query.order_by(Flight.flight_id.desc()).limit(limit + 1).all()

            has_more = len(rows) > limit
            rows = rows[:limit]
            ids = [f.flight_id for f, _ in rows]

            # Counted in two grouped queries over this page's ids rather than as
            # two joins on the main one: alerts and recordings both hang off
            # flights, so joining them together multiplies the rows and every
            # count comes back as the product of the two.
            alerts = self._counts_by_flight(session, Alert.flight_id, Alert.alert_id, ids)
            recordings = self._counts_by_flight(
                session, Recording.flight_id, Recording.recording_id, ids)

            return {
                "flights": [
                    {
                        "flight_id": f.flight_id,
                        "stream_id": s.stream_id,
                        "label": s.label,
                        "start_time": f.start_time,
                        # NULL means the flight was never closed out. Usually
                        # that is "still in the air", but it is also what a
                        # crashed orchestrator leaves behind, so the portal
                        # renders it as open rather than as live.
                        "end_time": f.end_time,
                        "alert_count": alerts.get(f.flight_id, 0),
                        "recording_count": recordings.get(f.flight_id, 0),
                    }
                    for f, s in rows
                ],
                # The cursor for the next page, or None at the end of history.
                "next_before": ids[-1] if (has_more and ids) else None,
            }

    @staticmethod
    def _counts_by_flight(session: Session, flight_column, pk_column, ids: List[int]) -> dict:
        """
        {flight_id: row count} over the given flights, or {} for no flights.

        The empty guard is not a micro-optimisation: `IN ()` is a syntax error on
        some backends and a query that matches nothing on others, and neither is
        worth finding out about from a page that renders every count as zero.
        """
        if not ids:
            return {}
        return dict(
            session.query(flight_column, func.count(pk_column))
            .filter(flight_column.in_(ids))
            .group_by(flight_column)
            .all()
        )

    def flight_detail(self, flight_id: int, user_id: int) -> Optional[dict]:
        """
        One of this user's flights, with its recordings and its most recent
        alerts. None if the flight does not exist OR belongs to someone else —
        the caller cannot tell which, and must not be able to.

        Alert IMAGE BYTES are deliberately absent. A flight with two hundred
        alerts holds two hundred JPEGs, and inlining them would make one page
        load tens of megabytes that the browser cannot cache separately or load
        lazily. Each alert reports whether it has one; alert_image() serves it.

        That was true of the RESPONSE and false of the QUERY until this was
        fixed, which is the more expensive half. `session.query(Alert)` selects
        every column, so the page fetched up to fifty full-resolution JPEGs out
        of the database and into this process purely to evaluate
        `image_data is not None` and throw them away. The alert image is the
        whole annotated frame at 1920x1080 (output_alert_streamer stores it
        unresized), so that is tens of megabytes per page view, on the endpoint
        whose docstring promised the opposite.

        The columns are therefore listed explicitly. Deferring the attribute
        instead would be worse: it fixes this query and turns any later
        `a.image_data` into one lazy SELECT per row.

        `alert_total` is the true count and `alerts` is at most one page of it,
        so a truncated list can be labelled as truncated rather than silently
        passing for the whole flight.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            row = (
                session.query(Flight, Stream)
                .join(Stream, Flight.stream_id == Stream.stream_id)
                .filter(Flight.flight_id == flight_id, Stream.user_id == user_id)
                .first()
            )
            if row is None:
                return None
            flight, stream = row

            alert_total = (session.query(func.count(Alert.alert_id))
                           .filter(Alert.flight_id == flight_id).scalar()) or 0

            # Newest first, matching the live alert aside — the two views of the
            # same flight should not disagree about which end is the top.
            #
            # Named columns, not the mapped entity: image_data must not be in
            # this result set. Whether a crop exists is answered by the database
            # as a boolean, so the bytes never leave it.
            alerts = (session.query(
                          Alert.alert_id,
                          Alert.alert_msg,
                          Alert.frame_id,
                          Alert.datetime,
                          Alert.image_width,
                          Alert.image_height,
                          Alert.image_data.isnot(None).label("has_image"),
                      )
                      .filter(Alert.flight_id == flight_id)
                      .order_by(Alert.alert_id.desc())
                      .limit(FLIGHT_ALERTS_PAGE_SIZE)
                      .all())

            recordings = (session.query(Recording)
                          .filter(Recording.flight_id == flight_id)
                          .order_by(Recording.recording_id)
                          .all())

            return {
                "flight_id": flight.flight_id,
                "stream_id": stream.stream_id,
                "label": stream.label,
                "start_time": flight.start_time,
                "end_time": flight.end_time,
                "alert_total": alert_total,
                "alerts": [
                    {
                        "alert_id": a.alert_id,
                        "alert_msg": a.alert_msg,
                        "frame_id": a.frame_id,
                        "datetime": a.datetime,
                        # Whether to render an <img> at all, without shipping the
                        # bytes to find out. Computed in SQL — see the query.
                        "has_image": bool(a.has_image),
                        "image_width": a.image_width,
                        "image_height": a.image_height,
                    }
                    for a in alerts
                ],
                "recordings": [
                    {
                        "recording_id": r.recording_id,
                        "storage_backend": r.storage_backend,
                        # Where the recorder put it. A blob name or a path on the
                        # recordings volume — not a URL anyone can open, which is
                        # why the portal shows it rather than linking it.
                        "storage_location": r.storage_location or r.segment_path,
                        "uploaded_at": r.uploaded_at,
                    }
                    for r in recordings
                ],
                # public_uuid is deliberately NOT returned. It names the flight's
                # media path, and history is a read of what happened, not a way
                # to reach the stream it happened on.
            }

    def alert_image(self, alert_id: int, flight_id: int, user_id: int) -> Optional[bytes]:
        """
        The JPEG crop stored with one alert, or None if there is no such alert,
        it carries no image, or it belongs to another user.

        Both ids are checked, not just alert_id: alert_ids are sequential across
        every tenant, so the flight in the URL has to be the alert's flight and
        the flight has to be the caller's. Either one alone would let a caller
        walk the id space and read other people's crops — which are photographs
        of somebody else's land.
        """
        SessionFactory = sessionmaker(bind=self._engine)
        with SessionFactory() as session:
            row = (
                session.query(Alert.image_data)
                .join(Flight, Alert.flight_id == Flight.flight_id)
                .join(Stream, Flight.stream_id == Stream.stream_id)
                .filter(Alert.alert_id == alert_id,
                        Alert.flight_id == flight_id,
                        Stream.user_id == user_id)
                .first()
            )
            return row[0] if row else None


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
