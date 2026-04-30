#!/bin/bash

# Streamlit UI Container Launch Script

# set -e  # Exit on any error

# paths for docker build
DOCKERFILE_PATH="./docker/ui/Dockerfile"
DOCKERIGNORE_PATH="./docker/ui/.dockerignore"
REQUIREMENTS_PATH="./docker/ui/requirements.txt"
ROOT_DOCKERIGNORE_PATH="./.dockerignore"
ROOT_REQUIREMENTS_PATH="./requirements.txt"


# Default values
IMAGE_NAME="agrarian-ui"
CONTAINER_NAME="agrarian-ui"
DETACHED="false"
REMOVE_EXISTING="true"
ENV_FILE=""
NETWORK=""
BUILD="false"


STREAM_PROTOCOL="webrtc"
STREAM_HOST="0.0.0.0"
STREAM_PORT=""           # resolved after arg parsing based on protocol
STREAM_NAME="annot"
STREAM_STUN_SERVER="stun:stun.l.google.com:19302"

WEBSOCKET_HOST="0.0.0.0"
WEBSOCKET_PORT="8443"
WEBSOCKET_RECONNECTION_DELAY="5"
WEBSOCKET_PING_INTERVAL="30"
WEBSOCKET_PING_TIMEOUT="10"

ALERTS_REFRESH="1.0"
ALERTS_BOX_COLOR_TIMEDIFF="5.0"
ALERTS_MAX_DISPLAYED="5"

LOGO="assets/leonardo.png"
LOGO_WIDTH="200"
HTML_HEIGHT="600"
ALERT_HEIGHT="600"

# Help function
show_help() {
    cat << EOF
Streamlit UI Container Launch Script

Usage: $0 [OPTIONS]

GENERAL OPTIONS:
    -i, --image NAME                Docker image name (default: $IMAGE_NAME)
    -n, --name NAME                 Container name (default: $CONTAINER_NAME)
    -d, --detached                  Run in detached mode
    -r, --remove                    Remove existing container if it exists
    -f, --env-file FILE             Load environment variables from file
    --network NETWORK               Connect to specific Docker network
    -b, --build                     Build image before running

STREAM & WEBSOCKET OPTIONS:
    --stream-protocol PROTO         Stream protocol: webrtc or hls (default: webrtc)
    --stream-host HOST              Stream server host (default: $STREAM_HOST)
    --stream-port PORT              Stream server port (default: 8889 for webrtc, 8888 for hls)
    --stream-name NAME              Stream name (default: $STREAM_NAME)
    --stun-server SERVER            STUN server, webrtc only (default: $STREAM_STUN_SERVER)
    --ws-host HOST                  WebSocket Host (default: $WEBSOCKET_HOST)
    --ws-port PORT                  WebSocket Port (default: $WEBSOCKET_PORT)

UI & ALERTS OPTIONS:
    --logo PATH                     Path to logo (default: $LOGO)
    --alerts-max NUM                Max alerts displayed (default: $ALERTS_MAX_DISPLAYED)
    --html-height PX                UI HTML Height (default: $HTML_HEIGHT)

    -h, --help                      Show this help message
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--image) IMAGE_NAME="$2"; shift 2 ;;
        -n|--name)  CONTAINER_NAME="$2"; shift 2 ;;
        -d|--detached) DETACHED="true"; shift ;;
        -r|--remove) REMOVE_EXISTING="true"; shift ;;
        -f|--env-file) ENV_FILE="$2"; shift 2 ;;
        --network) NETWORK="$2"; shift 2 ;;
        -b|--build) BUILD="true"; shift ;;

        # Stream Mappings
        --stream-protocol) STREAM_PROTOCOL="$2"; shift 2 ;;
        --stream-host) STREAM_HOST="$2"; shift 2 ;;
        --stream-port) STREAM_PORT="$2"; shift 2 ;;
        --stream-name) STREAM_NAME="$2"; shift 2 ;;
        --stun-server) STREAM_STUN_SERVER="$2"; shift 2 ;;

        # WebSocket Mappings
        --ws-host) WEBSOCKET_HOST="$2"; shift 2 ;;
        --ws-port) WEBSOCKET_PORT="$2"; shift 2 ;;
        --ws-delay) WEBSOCKET_RECONNECTION_DELAY="$2"; shift 2 ;;

        # UI/Alerts Mappings
        --alerts-refresh) ALERTS_REFRESH="$2"; shift 2 ;;
        --alerts-max) ALERTS_MAX_DISPLAYED="$2"; shift 2 ;;
        --logo) LOGO="$2"; shift 2 ;;
        --logo-width) LOGO_WIDTH="$2"; shift 2 ;;
        --html-height) HTML_HEIGHT="$2"; shift 2 ;;
        --alert-height) ALERT_HEIGHT="$2"; shift 2 ;;

        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# Validate protocol
if [[ "$STREAM_PROTOCOL" != "webrtc" && "$STREAM_PROTOCOL" != "hls" ]]; then
    echo "Invalid stream protocol: $STREAM_PROTOCOL. Must be 'webrtc' or 'hls'."
    exit 1
fi

# Resolve default port based on protocol if not explicitly set
if [[ -z "$STREAM_PORT" ]]; then
    if [[ "$STREAM_PROTOCOL" == "hls" ]]; then
        STREAM_PORT="8888"
    else
        STREAM_PORT="8889"
    fi
fi


# Build image if requested
if [[ "$BUILD" == "true" ]]; then
    echo "Building Docker image: $IMAGE_NAME"
    # copy .dockerignore to context root for build
    cp "$DOCKERIGNORE_PATH" "$ROOT_DOCKERIGNORE_PATH"
    # copy requirements.txt to context root for build
    cp "$REQUIREMENTS_PATH" "$ROOT_REQUIREMENTS_PATH"
    # build docker image
    docker build -f "$DOCKERFILE_PATH" -t "$IMAGE_NAME" .
    # remove .dockerignore copy from context root
    rm "$ROOT_DOCKERIGNORE_PATH"
    # remove requirements.txt copy from context root
    rm "$ROOT_REQUIREMENTS_PATH"
fi

# Remove existing container if requested
if [[ "$REMOVE_EXISTING" == "true" ]]; then
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "Removing existing container: $CONTAINER_NAME"
        docker rm -f "$CONTAINER_NAME"
    fi
fi

# Check if container already exists and is running
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Container $CONTAINER_NAME is already running!"
    echo "Use -r/--remove flag to remove and recreate, or choose a different name."
    exit 1
fi

# Build docker run command
DOCKER_CMD="docker run"

# Add detached mode if requested
if [[ "$DETACHED" == "true" ]]; then
    DOCKER_CMD="$DOCKER_CMD -d"
else
    DOCKER_CMD="$DOCKER_CMD -it"
fi

# Add container name
DOCKER_CMD="$DOCKER_CMD --name $CONTAINER_NAME"

# Add port mappings
DOCKER_CMD="$DOCKER_CMD -p 8501:8501"

# Add network if specified
if [[ -n "$NETWORK" ]]; then
    DOCKER_CMD="$DOCKER_CMD --network $NETWORK"
fi

# --- Construct Docker Environment Flags ---

# Stream Environment Variables
DOCKER_CMD="$DOCKER_CMD -e STREAM_PROTOCOL=$STREAM_PROTOCOL"
DOCKER_CMD="$DOCKER_CMD -e STREAM_HOST=$STREAM_HOST"
DOCKER_CMD="$DOCKER_CMD -e STREAM_PORT=$STREAM_PORT"
DOCKER_CMD="$DOCKER_CMD -e STREAM_NAME=$STREAM_NAME"
if [[ "$STREAM_PROTOCOL" == "webrtc" ]]; then
    DOCKER_CMD="$DOCKER_CMD -e STREAM_STUN_SERVER=$STREAM_STUN_SERVER"
fi

# WebSocket Environment Variables
DOCKER_CMD="$DOCKER_CMD -e WEBSOCKET_HOST=$WEBSOCKET_HOST"
DOCKER_CMD="$DOCKER_CMD -e WEBSOCKET_PORT=$WEBSOCKET_PORT"
DOCKER_CMD="$DOCKER_CMD -e WEBSOCKET_RECONNECTION_DELAY=$WEBSOCKET_RECONNECTION_DELAY"
DOCKER_CMD="$DOCKER_CMD -e WEBSOCKET_PING_INTERVAL=$WEBSOCKET_PING_INTERVAL"
DOCKER_CMD="$DOCKER_CMD -e WEBSOCKET_PING_TIMEOUT=$WEBSOCKET_PING_TIMEOUT"

# Alerts Environment Variables
DOCKER_CMD="$DOCKER_CMD -e ALERTS_REFRESH=$ALERTS_REFRESH"
DOCKER_CMD="$DOCKER_CMD -e ALERTS_BOX_COLOR_TIMEDIFF=$ALERTS_BOX_COLOR_TIMEDIFF"
DOCKER_CMD="$DOCKER_CMD -e ALERTS_MAX_DISPLAYED=$ALERTS_MAX_DISPLAYED"

# UI Configuration Environment Variables
DOCKER_CMD="$DOCKER_CMD -e LOGO=$LOGO"
DOCKER_CMD="$DOCKER_CMD -e LOGO_WIDTH=$LOGO_WIDTH"
DOCKER_CMD="$DOCKER_CMD -e HTML_HEIGHT=$HTML_HEIGHT"
DOCKER_CMD="$DOCKER_CMD -e ALERT_HEIGHT=$ALERT_HEIGHT"


# Map logs folder
DOCKER_CMD="$DOCKER_CMD -v $(pwd)/logs:/app/logs"


# Add env file if specified
if [[ -n "$ENV_FILE" ]]; then
    if [[ -f "$ENV_FILE" ]]; then
        DOCKER_CMD="$DOCKER_CMD --env-file $ENV_FILE"
    else
        echo "Environment file not found: $ENV_FILE"
        exit 1
    fi
fi

# Add image name
DOCKER_CMD="$DOCKER_CMD $IMAGE_NAME"

# Display configuration
echo "================================================"
echo "Streamlit UI Container Configuration"
echo "================================================"
echo "GENERAL SETTINGS:"
echo "  Image Name:           $IMAGE_NAME"
echo "  Container Name:       $CONTAINER_NAME"
echo "  Detached Mode:        $DETACHED"
echo "  Network:              ${NETWORK:-default}"
echo "  Environment File:     ${ENV_FILE:-none}"
echo "  Build Image:          $BUILD"
echo "------------------------------------------------"
echo "STREAM & WEBSOCKET:"
echo "  Stream Protocol:      $STREAM_PROTOCOL"
echo "  Stream Host/Port:     $STREAM_HOST:$STREAM_PORT"
echo "  Stream Name:          $STREAM_NAME"
if [[ "$STREAM_PROTOCOL" == "webrtc" ]]; then
echo "  STUN Server:          $STREAM_STUN_SERVER"
fi
echo "  WebSocket:            $WEBSOCKET_HOST:$WEBSOCKET_PORT"
echo "  WS Reconnect Delay:   ${WEBSOCKET_RECONNECTION_DELAY}s"
echo "------------------------------------------------"
echo "ALERTS & UI:"
echo "  Alerts Refresh:       ${ALERTS_REFRESH}s"
echo "  Max Displayed:        $ALERTS_MAX_DISPLAYED"
echo "  Logo Path:            $LOGO"
echo "  HTML Height:          ${HTML_HEIGHT}px"
echo "  Alerts Height:        ${ALERT_HEIGHT}px"
echo "================================================"


# Ask for confirmation unless in detached mode
if [[ "$DETACHED" != "true" ]]; then
    read -p "Launch container with these settings? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Launch cancelled."
        exit 0
    fi
fi

# Execute docker run command
echo "Launching container..."
echo "Command: $DOCKER_CMD"
echo ""

exec $DOCKER_CMD
