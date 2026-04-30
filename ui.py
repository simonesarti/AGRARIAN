import streamlit as st
from collections import deque
import logging
import streamlit.components.v1 as components
import datetime
import os
from time import time
from src.ui.alert_receiver import AlertReceiver
from src.ui.webrtc_video_player import get_video_player as get_webrtc_player
from src.ui.hls_video_player import get_video_player as get_hls_player

# ================================================================
# Logging Configuration
# ================================================================
log_path = "./logs/ui.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_path, mode='w')]
)

logger = logging.getLogger("ui")


# ================================================================
# Application constants
# ================================================================

STREAM_PROTOCOL = os.getenv("STREAM_PROTOCOL", "webrtc")
STREAM_HOST = os.getenv("STREAM_HOST", "0.0.0.0")
STREAM_PORT = int(os.getenv("STREAM_PORT", 8889))
STREAM_NAME = os.getenv("STREAM_NAME", "annot")
STREAM_STUN_SERVER = os.getenv("STREAM_STUN_SERVER", "stun:stun.l.google.com:19302")

WEBSOCKET_HOST = os.getenv("WEBSOCKET_HOST", "0.0.0.0")
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", 8443))
WEBSOCKET_RECONNECTION_DELAY = int(os.getenv("WEBSOCKET_RECONNECTION_DELAY", 5))
WEBSOCKET_PING_INTERVAL = int(os.getenv("WEBSOCKET_PING_INTERVAL", 30))
WEBSOCKET_PING_TIMEOUT = int(os.getenv("WEBSOCKET_PING_TIMEOUT", 10))

ALERTS_REFRESH = float(os.getenv("ALERTS_REFRESH", 1.0))
ALERTS_BOX_COLOR_TIMEDIFF = float(os.getenv("ALERTS_BOX_COLOR_TIMEDIFF", 5.0))
ALERTS_MAX_DISPLAYED = int(os.getenv("ALERTS_MAX_DISPLAYED", 5))

LOGO = os.getenv("LOGO", "assets/leonardo.png")
LOGO_WIDTH = int(os.getenv("LOGO_WIDTH", 200))
HTML_HEIGHT = int(os.getenv("HTML_HEIGHT", 600))
ALERT_HEIGHT = int(os.getenv("ALERT_HEIGHT", 600))

STREAM_URL = f"http://{STREAM_HOST}:{STREAM_PORT}"
WEBSOCKET_URL = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}"


# ================================================================
# initialization
# ================================================================

def initialize_services() -> bool:
    """Initialize all services automatically"""

    initialized = False

    if 'services_initialized' not in st.session_state:

        st.session_state.services_initialized = True

        # Initialize session state
        st.session_state.alerts_display_dequeue = deque(maxlen=ALERTS_MAX_DISPLAYED)
        st.session_state.total_alerts = 0
        st.session_state.last_alert_timestamp = None

        stream_config = {
            'protocol': STREAM_PROTOCOL,
            'url': STREAM_URL,
            'name': STREAM_NAME,
        }
        if STREAM_PROTOCOL == "webrtc":
            stream_config['stun'] = STREAM_STUN_SERVER
        logger.info(f"Stream configured as follows: {stream_config}")

        # Start WEBSOCKET receiver
        st.session_state.websocket_receiver = AlertReceiver(
            host=WEBSOCKET_HOST,
            port=WEBSOCKET_PORT,
            shared_dequeue=st.session_state.alerts_display_dequeue,
            reconnection_delay=WEBSOCKET_RECONNECTION_DELAY,
            ping_interval=WEBSOCKET_PING_INTERVAL,
            ping_timeout=WEBSOCKET_PING_TIMEOUT,
        )
        st.session_state.ws_thread = st.session_state.websocket_receiver.start()
        logger.info(
            f"Websocket client initialized and started. "
            f"Waiting for alerts from {WEBSOCKET_URL}"
        )

        initialized = True

    return initialized


# ================================================================
# Alert Processing Fragments
# ================================================================

@st.fragment(run_every=ALERTS_REFRESH)
def process_alerts():
    """Process new alerts in a fragment that runs independently"""

    # Display alerts
    if st.session_state.websocket_receiver.get_total_alerts() > 0:
        # Create a container with fixed height and scrollable content
        with st.container(height=ALERT_HEIGHT):  # Adjust height as needed (in pixels)
            dequeue_snapshot = list(st.session_state.alerts_display_dequeue)
            for i, alert in enumerate(dequeue_snapshot):

                alert_timestamp = alert['timestamp']

                # the leftmost alert is the most recent (appendleft)
                if i == 0:
                    st.session_state.last_alert_timestamp = alert_timestamp  # save last alert time() UTC

                alert_local_time = (
                    datetime.datetime
                        .fromtimestamp(alert_timestamp, tz=datetime.timezone.utc)
                        .astimezone()
                        .strftime('%Y-%m-%d %H:%M:%S')
                )
                
                st.error(f"**Alert:** {alert['alert_msg']}")
                if alert['image'] is not None:  # None when decoding error
                    st.image(
                        alert['image'],
                        use_container_width=True,
                        #width='stretch',
                        caption=f"Frame {alert['frame_id']} - {alert_local_time}"
                    )
                
                if i < len(dequeue_snapshot) - 1:
                    st.divider()
    else:
        st.info("📭 No alerts received yet")


@st.fragment(run_every=ALERTS_REFRESH)
def update_metrics():

    if st.session_state.last_alert_timestamp is None:
        st.success("No alerts received yet")

    else:
        current_time = time()
        logger.debug(f"current UTC time: {current_time}")
        logger.debug(f"last alert UTC time: {st.session_state.last_alert_timestamp}")

        # compute time difference in UTC time
        seconds_passed = int(current_time - st.session_state.last_alert_timestamp)
        logger.info(f"seconds passed: {seconds_passed}")
        minutes_passed = seconds_passed // 60
        seconds_passed = seconds_passed % 60

        # Convert time to local timestamp for displaying
        last_alert_local = (
            datetime.datetime
                .fromtimestamp(st.session_state.last_alert_timestamp, tz=datetime.timezone.utc)
                .astimezone()
                .strftime('%H:%M:%S')
        )

        logger.info(f"last alert local time {last_alert_local}")

        if minutes_passed >= 1:
            text = f"Alert received {minutes_passed}:{seconds_passed} minutes ago ({last_alert_local})"
        else:
            text = f"Alert received {seconds_passed} seconds ago ({last_alert_local})"

        if seconds_passed > ALERTS_BOX_COLOR_TIMEDIFF:
            st.warning(text)  # yellow if older
        else:
            st.error(text)  # red if very recent

    st.metric("Total Alerts", st.session_state.websocket_receiver.get_total_alerts())
    st.metric("Displayed Alerts", len(st.session_state.alerts_display_dequeue))


# ================================================================
# Main Application
# ================================================================

# main reruns at every user interaction
def main():
    
    # Configure page
    st.set_page_config(
        page_title="Video Stream & Alerts Monitor",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Initialize services (returns True only on first initialization / main loop)
    initialized = initialize_services()

    # Main content
    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.header("🎥 Live Video Stream")

        # Only build the player HTML on startup
        if initialized:
            if STREAM_PROTOCOL == "webrtc":
                html_video_player = get_webrtc_player(
                    webrtc_url=STREAM_URL,
                    stream_name=STREAM_NAME,
                    stun_server=STREAM_STUN_SERVER,
                )
            else:
                html_video_player = get_hls_player(
                    hls_url=STREAM_URL,
                    stream_name=STREAM_NAME,
                )
            st.session_state.stream_html = html_video_player
            logger.info(f"Stream component rendered (protocol: {STREAM_PROTOCOL})")

        # Render the component using cached state
        components.html(st.session_state.stream_html, height=HTML_HEIGHT)
    
    with right_col:
        st.header("🚨 Alert Feed")
        # Process alerts in a fragment that runs independently
        process_alerts()

    # Sidebar configuration
    with st.sidebar:
        st.image(LOGO, width=LOGO_WIDTH)

        st.subheader("Video stream configuration")
        st.text_input(
            "Protocol",
            value=STREAM_PROTOCOL.upper(),
            disabled=True,
        )
        st.text_input(
            "Stream URL",
            value=STREAM_URL,
            help="The stream server URL",
            disabled=True,
        )
        st.text_input(
            "Stream name",
            value=STREAM_NAME,
            disabled=True,
        )
        if STREAM_PROTOCOL == "webrtc":
            st.text_input(
                "STUN server",
                value=STREAM_STUN_SERVER,
                disabled=True,
            )

        st.subheader("Alerts stream configuration")
        st.text_input(
            "WEBSOCKET server URL",
            value=WEBSOCKET_URL,
            help="The URL of the Websocket server sending alerts",
            disabled=True,
        )

        st.divider()

        # Process alerts metrics in a fragment that runs independently
        update_metrics()

        if st.button("Clear displayed alerts", type="secondary"):
            st.session_state.alerts_display_dequeue.clear()
            logger.info("Alerts cleared")


if __name__ == "__main__":
    main()
