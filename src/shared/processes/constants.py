# -------------------------- APPLICATION MODES --------------------------

SUPPORTED_APP_MODES = ("danger_detection", "health_monitoring")

# -------------------------- GENERAL --------------------------

# target FPS for processing and output
FPS = 30

ALERTS_COOLDOWN_SECONDS = 1.0                                          # 1 second

# value of the poison pill to stop following processes
POISON_PILL = "HALT"
POISON_PILL_TIMEOUT = 5.0                                               # 5.0 s
SHUTDOWN_TIMEOUT = 10.0                                                 # 10.0 s


# str: Name of the directory where the results of the analysis will be saved
LOCAL_OUTPUT_DIR = 'processing_results'

# str: Name of the video showing the annotated original data (with/without sheep count and tracks)
CODEC = 'mp4v'

# agrarian database name
DB_NAME = "agrarian_db"

# -------------------------- DANGER DETECTION PARAMETERS --------------------------

# float: radius of the of the safety around each sheep/goats, in meters
SAFETY_RADIUS_M = 2.0                                                      # meters

# float: slope angle after which the portion of terrain is considered dangerous for the animals
SLOPE_ANGLE_THRESHOLD = 30.0

# -------------------------- HEALTH MONITORING PARAMETERS --------------------------

# Anomaly detection defaults
HM_ANOMALY_USE_AE = True
HM_ANOMALY_USE_SOCIAL = True
HM_ANOMALY_AE_THRESHOLD = 2.75
HM_ANOMALY_SOCIAL_THRESHOLD = 5.0
HM_ANOMALY_SMOOTHING_WINDOW = 56
HM_ANOMALY_MIN_ANOMALY_DURATION = 90
HM_ANOMALY_SOCIAL_EMA_ALPHA = 0.007
HM_ANOMALY_SOCIAL_MIN_UPDATES = 375
HM_ANOMALY_SOCIAL_MIN_HERD = 5
HM_ANOMALY_REQUIRE_BOTH = False

# -------------------------- DRONE HARDWARE PARAMETERS --------------------------

# https://sdk-forum.dji.net/hc/en-us/articles/12325496609689-What-is-the-custom-camera-parameters-for-Mavic-3-Enterprise-series-and-Mavic-3M

# positive float: true focal lenght of the camera in mm
DRONE_TRUE_FOCAL_LEN_MM = 12.29

# positive float: width of the camera sensor in millimeters
DRONE_SENSOR_WIDTH_MM = 17.35 # standard for 4/3 CMOS sensor

# positive float: height of the camera sensor in millimeters
DRONE_SENSOR_HEIGHT_MM = 13.00  # standard for 4/3 CMOS sensor

# positive int: width of the camera sensor in pixels
DRONE_SENSOR_WIDTH_PIXELS = 5280 # standard Effective 20MP for 4/3 CMOS sensor

# positive int: height of the camera sensor in pixels
DRONE_SENSOR_HEIGHT_PIXELS = 3956  # standard Effective 20MP for 4/3 CMOS sensor

# NOTE: sensor_width_pixels/sensor_height_pixels MUST be equal to sensor_width_mm/sensor_height_mm!!!!

# -------------------------- PROTOCOLS --------------------------

ALL_INTERFACES = "0.0.0.0"

HTTP_PORT = 80
HTTPS_PORT = 8443
MQTT_PORT = 1883
MQTTS_PORT = 8883
RTMP_PORT = 1935
RTMPS_PORT = 8443
RTSP_PORT = 8554
RTSPS_PORT = 441
WEBRTC_PORT = 8889
WS_PORT = 80
WSS_PORT = 8443
WS_COMMON_PORT = 8765
DB_COMMON_PORT = 5432

# -------------------------- PROCESSES QUEUES SIZES --------------------------

MAX_SIZE_FRAME_READER_OUT=3
MAX_SIZE_DETECTION_IN=3
MAX_SIZE_SEGMENTATION_IN=3
MAX_SIZE_GEO_IN=3
MAX_SIZE_DANGER_DETECTION_RESULT=3
MAX_SIZE_VIDEO_STREAM=3
MAX_SIZE_NOTIFICATIONS_STREAM=5

# ------------------------ VIDEO READING ----------

# VIDEO_STREAM_URL = "rtmp://<server>[:port]/<app>/<stream_key>"
# VIDEO_STREAM_URL = "rtmps://<server>[:port]/<app>/<stream_key>"
# VIDEO_STREAM_URL = "rtsp://[user[:password]@]host[:port]/path"
# VIDEO_STREAM_URL = "rtsps://[user[:password]@]host[:port]/path"

VIDEO_STREAM_READER_HOST = ALL_INTERFACES
VIDEO_STREAM_READER_PORT = RTSP_PORT
VIDEO_STREAM_READER_STREAM_KEY = "drone"

VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S = 5.0
VIDEO_STREAM_READER_RECONNECT_DELAY = 5.0
VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES = 5

VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S = 0.05                         # 50 ms
VIDEO_STREAM_READER_FRAME_RETRY_DELAY = 0.05                            # 50 ms
VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES = FPS                # 1 second worth of failures

VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO = 16.0/9.0
VIDEO_STREAM_READER_PROCESSING_SHAPE = (1280, 720)  # (W,H)
VIDEO_STREAM_READER_ORIGINAL_SHAPE = (1920, 1080)   # (W,H) expected original resolution for output buffer pre-allocation



# -------------------------- TELEMETRY READER --------------------------

TELEMETRY_LISTENER_HOST = ALL_INTERFACES
TELEMETRY_LISTENER_PORT = MQTT_PORT

# QoS 0 (At most once): no acknowledgment from the receiver
# QoS 1 (At least once):  ensures that messages are delivered at least once by requiring a PUBACK acknowledgment
# QoS 2 (Exactly once): guarantees that each message is delivered exactly once by using a four-step handshake
# (PUBLISH, PUBREC, PUBREL, PUBCOMP)
TELEMETRY_LISTENER_QOS_LEVEL = 1

# Seconds to wait before attempting reconnection
TELEMETRY_LISTENER_RECONNECT_DELAY = 5.0
# max thread blocking message wait, after this, check again wheter a stop signal has been received
TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT = 1.0
# size of the input messages queue
TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES = 100


TELEMETRY_LISTENER_TOPICS_TO_SUBSCRIBE = [
    "telemetry/latitude",
    "telemetry/longitude",
    "telemetry/rel_alt",
    "telemetry/gb_yaw",
]

TELEMETRY_LISTENER_TOPICS_TO_TELEMETRY_MAPPING = {
    "telemetry/latitude": "latitude",
    "telemetry/longitude": "longitude",
    "telemetry/rel_alt": "rel_alt",
    "telemetry/gb_yaw": "gb_yaw",
}

TELEMETRY_LISTENER_TEMPLATE_TELEMETRY = {
    "latitude": 44.414622942776454,
    "longitude": 8.880484631296774,
    "rel_alt": 40.0,
    "gb_yaw": 270.0,
}

# -------------------------------------------------------------------
# -------------------------- FRAME + TELEMETRY COMBINING ------------
# -------------------------------------------------------------------
FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE = 40
FRAMETELCOMB_MAX_TIME_DIFF = 0.15                   # 150 ms
# -------------------------------------------------------------------
# -------------------------- PIPELINE QUEUE TIMEOUT -----------------
# -------------------------------------------------------------------
# Single timeout used by all hot-path stages (stream reader, combiner,
# models, annotation, video writer, video streamer) for both get and put
# blocking calls. Controls shutdown responsiveness, not correctness.
PIPELINE_QUEUE_TIMEOUT = 0.01                       # 10 ms

# -------------------------------------------------------------------
# -------------------------- MODELS & ANNOTATIONS -------------------
# -------------------------------------------------------------------
ANNOTATION_MAX_PUT_ALERT_CONSECUTIVE_FAILURES = 3
ANNOTATION_MAX_PUT_VIDEO_CONSECUTIVE_FAILURES = FPS * 2

# -------------------------------------------------------------------
# -------------------------- ALERTS WRITER --------------------------
# -------------------------------------------------------------------

ALERTS_QUEUE_GET_TIMEOUT = 0.1                                # 100 ms

ALERTS_MAX_CONSECUTIVE_FAILURES = 5
ALERTS_JPEG_COMPRESSION_QUALITY = 85

# -------------------------- ALERTS WS --------------------------

WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = HTTPS_PORT

WS_MANAGER_BROADCAST_TIMEOUT = 2.0
WS_MANAGER_PING_INTERVAL = 5.0                          # 5.0 s
WS_MANAGER_PING_TIMEOUT = 20.0                          # 20.0 s
WS_MANAGER_THREAD_CLOSE_TIMEOUT = 5.0                   # 5.0 s

# -------------------------- ALERTS DB --------------------------

DB_HOST = ALL_INTERFACES
DB_PORT = DB_COMMON_PORT                  

DB_MANAGER_QUEUE_SIZE = 5

DB_MANAGER_POOL_SIZE = 5
DB_MANAGER_MAX_OVERFLOW = 10

DB_MANAGER_QUEUE_WAIT_TIMEOUT = 0.1                     # 100 ms
DB_MANAGER_THREAD_CLOSE_TIMEOUT = 5.0                   # 5.0 s


# -------------------------------------------------------------------
# -------------------------- OUT VIDEO WRITER --------------------------
# -------------------------------------------------------------------

VIDEO_WRITER_HANDOFF_TIMEOUT = 1.0

# ------------------------- OUT VIDEO STREAM  --------------------------

VIDEO_OUT_STREAM_HOST = ALL_INTERFACES
VIDEO_OUT_STREAM_PORT = RTMP_PORT
VIDEO_OUT_STREAM_STREAM_KEY = "annot"

VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT = 0.5               # 0.5 s
VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT = 8.0              # 8.0 s
VIDEO_OUT_STREAM_STARTUP_TIMEOUT = 2.0                      # 2.0 s
VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT = 5.0                     # 5.0 s

# ------------------------- OUT VIDEO STORE  --------------------------

VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS = True
VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT = 3.0                     # 3.0 s
VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES = 3                      # 3 attempts
VIDEO_OUT_STORE_RETRY_BACKOFF_TIME = 5.0                    # 5 s

# Local storage (testing / fallback)
VIDEO_OUT_STORE_LOCAL_TARGET_DIR = LOCAL_OUTPUT_DIR
