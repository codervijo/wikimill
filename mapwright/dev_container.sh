#!/bin/bash

set -euo pipefail

# Configuration
CONTAINER="${CONTAINER_NAME:-mwai1}"
HOST_PORT="${HOST_PORT:-3400}"
CONTAINER_PORT="${CONTAINER_PORT:-3445}"
VOLUME_MOUNT="${VOLUME_MOUNT:-${PWD}:/usr/src/app/}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-.}"
DOCKER_CMD="${DOCKER_CMD:-sudo docker}"

# X11 settings for headed UI support
XSOCK="/tmp/.X11-unix"
XAUTH="/tmp/.docker.xauth"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Check OS compatibility
check_os() {
    if [[ "$OSTYPE" != "linux-gnu"* ]]; then
        print_error "This script only supports Linux/Ubuntu systems"
        print_error "Detected OS: $OSTYPE"
        exit 1
    fi
}

# Check dependencies
check_dependencies() {
    local missing_deps=()
    
    if ! command -v docker &> /dev/null; then
        missing_deps+=("docker")
    fi
    
    if ! command -v xauth &> /dev/null; then
        missing_deps+=("xauth")
    fi
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing dependencies: ${missing_deps[*]}"
        print_error "Please install the missing dependencies:"
        print_error "  sudo apt update"
        print_error "  sudo apt install docker.io xauth"
        exit 1
    fi
}

# Check if running with appropriate privileges
check_privileges() {
    if ! $DOCKER_CMD ps &> /dev/null; then
        print_error "Cannot access Docker. Please ensure:"
        print_error "1. Docker is running"
        print_error "2. You have appropriate permissions (try adding your user to docker group)"
        print_error "3. Or run with sudo"
        exit 1
    fi
}

# Usage
usage() {
    cat << EOF
Usage: $0 [COMMAND]

Commands:
    start       Build image (if needed) and start container
    shell       Open shell in running container
    stop        Stop running container
    delete      Stop and remove container and image
    logs        Show container logs
    status      Show container status
    rebuild     Force rebuild image and restart container

Environment Variables:
    CONTAINER_NAME    Container name (default: mwai1)
    HOST_PORT        Host port (default: 3400)
    CONTAINER_PORT   Container port (default: 3445)
    VOLUME_MOUNT     Volume mount (default: \${PWD}:/usr/src/app/)
    DOCKERFILE_PATH  Path to Dockerfile (default: .)
    DOCKER_CMD       Docker command (default: sudo docker)

Examples:
    $0                    # Build and start, then open shell
    $0 start             # Just build and start
    CONTAINER_NAME=myapp $0 start  # Use custom container name
EOF
    exit 1
}

# Build image with better error handling
build_image() {
    print_info "Building image..."
    
    if [ ! -f "${DOCKERFILE_PATH}/Dockerfile" ]; then
        print_error "Dockerfile not found at ${DOCKERFILE_PATH}/Dockerfile"
        exit 1
    fi
    
    # Try build with cache first, fallback to clean build
    if ! $DOCKER_CMD build \
        --tag "${CONTAINER}:latest" \
        --cache-from "${CONTAINER}:latest" \
        -t "${CONTAINER}" \
        "${DOCKERFILE_PATH}" 2>/dev/null; then
        
        print_warning "Build with cache failed, trying clean build..."
        if ! $DOCKER_CMD build -t "${CONTAINER}" "${DOCKERFILE_PATH}"; then
            print_error "Docker build failed"
            exit 1
        fi
    fi
    
    print_success "Image built successfully"
}

# Check if image exists
image_exists() {
    $DOCKER_CMD images -q "${CONTAINER}" 2>/dev/null | grep -q .
}

# Check if container exists
container_exists() {
    $DOCKER_CMD ps -aq -f name="^${CONTAINER}$" 2>/dev/null | grep -q .
}

# Check if container is running
container_running() {
    $DOCKER_CMD ps -q -f name="^${CONTAINER}$" 2>/dev/null | grep -q .
}

# Get container status
get_container_status() {
    if container_running; then
        echo "running"
    elif container_exists; then
        echo "stopped"
    else
        echo "not_found"
    fi
}

# Set up X11 access with error handling
setup_x11() {
    if [ -z "${DISPLAY:-}" ]; then
        print_warning "DISPLAY not set, X11 applications may not work"
        return 0
    fi
    
    print_info "Setting up X11 access..."
    
    # Create X11 auth file if it doesn't exist
    if [ ! -f "${XAUTH}" ]; then
        if [ -d "${XAUTH}" ]; then
            rm -rf "${XAUTH}"
        fi
        if ! touch "${XAUTH}" 2>/dev/null; then
            print_error "Cannot create X11 auth file at ${XAUTH}"
            print_error "Please ensure /tmp is writable"
            exit 1
        fi
        
        if ! xauth nlist "$DISPLAY" 2>/dev/null | sed -e 's/^..../ffff/' | xauth -f "${XAUTH}" nmerge - 2>/dev/null; then
            print_warning "Failed to set up X11 authentication"
        fi
    fi
    
    # Set up xhost permissions
    if ! xhost +SI:localuser:root >/dev/null 2>&1; then
        print_warning "Failed to set xhost permissions, GUI applications may not work"
    fi
}

# Build X11 docker args
get_x11_args() {
    local args=""
    
    if [ -n "${DISPLAY:-}" ]; then
        if [ -S "${XSOCK}" ]; then
            args+=" -v ${XSOCK}:${XSOCK}"
        fi
        if [ -f "${XAUTH}" ]; then
            args+=" -v ${XAUTH}:${XAUTH}"
            args+=" -e XAUTHORITY=${XAUTH}"
        fi
        args+=" -e DISPLAY=${DISPLAY}"
    fi
    
    echo "$args"
}

# Start container with comprehensive error handling
start_container() {
    local status=$(get_container_status)
    
    case "$status" in
        "running")
            print_info "Container ${CONTAINER} is already running"
            return 0
            ;;
        "stopped")
            print_info "Container ${CONTAINER} exists but is not running. Starting it..."
            if $DOCKER_CMD start "${CONTAINER}"; then
                print_success "Container ${CONTAINER} started"
                return 0
            else
                print_error "Failed to start existing container"
                exit 1
            fi
            ;;
        "not_found")
            print_info "Creating new container..."
            ;;
    esac
    
    # Ensure image exists
    if ! image_exists; then
        print_error "Image ${CONTAINER} does not exist. Please build it first."
        exit 1
    fi
    
    setup_x11
    
    # Build docker run command
    local docker_args=(
        "run" "-d"
        "-v" "${VOLUME_MOUNT}"
        "--name" "${CONTAINER}"
        "--cap-add=SYS_ADMIN"
        "--security-opt" "seccomp=unconfined"
    )
    
    # Add X11 args if available
    local x11_args=$(get_x11_args)
    if [ -n "$x11_args" ]; then
        # Split x11_args properly
        eval "docker_args+=($x11_args)"
    fi
    
    # Add network and ports
    if [ -n "${HOST_PORT}" ] && [ -n "${CONTAINER_PORT}" ]; then
        docker_args+=("-p" "${HOST_PORT}:${CONTAINER_PORT}")
    else
        docker_args+=("--network=host")
    fi
    
    # Add image and command
    docker_args+=("${CONTAINER}" "tail" "-f" "/dev/null")
    
    # Run container
    if $DOCKER_CMD "${docker_args[@]}"; then
        print_success "Container ${CONTAINER} started"
    else
        print_error "Failed to start container"
        exit 1
    fi
}

# Execute shell with error handling
exec_shell() {
    if ! container_running; then
        print_error "Container ${CONTAINER} is not running"
        print_info "Run '$0 start' to start the container first"
        exit 1
    fi
    
    print_info "Opening shell in container ${CONTAINER}..."
    
    # Try bash first, fallback to sh
    if ! $DOCKER_CMD exec -it "${CONTAINER}" /bin/bash 2>/dev/null; then
        print_warning "Bash not available, trying sh..."
        if ! $DOCKER_CMD exec -it "${CONTAINER}" /bin/sh; then
            print_error "Failed to open shell in container"
            exit 1
        fi
    fi
}

# Stop container
stop_container() {
    if ! container_running; then
        print_warning "Container ${CONTAINER} is not running"
        return 0
    fi
    
    print_info "Stopping container ${CONTAINER}..."
    if $DOCKER_CMD stop "${CONTAINER}"; then
        print_success "Container ${CONTAINER} stopped"
    else
        print_error "Failed to stop container"
        exit 1
    fi
}

# Delete container and image
delete_container() {
    local status=$(get_container_status)
    
    if [ "$status" = "not_found" ]; then
        print_warning "Container ${CONTAINER} does not exist"
        if image_exists; then
            print_info "Removing orphaned image..."
            $DOCKER_CMD rmi "${CONTAINER}" && print_success "Image removed"
        fi
        return 0
    fi
    
    # Stop if running
    if [ "$status" = "running" ]; then
        stop_container
    fi
    
    # Remove container
    print_info "Removing container ${CONTAINER}..."
    if $DOCKER_CMD rm "${CONTAINER}"; then
        print_success "Container removed"
    else
        print_error "Failed to remove container"
        exit 1
    fi
    
    # Remove image
    if image_exists; then
        print_info "Removing image ${CONTAINER}..."
        if $DOCKER_CMD rmi "${CONTAINER}"; then
            print_success "Image removed"
        else
            print_warning "Failed to remove image (may be in use by other containers)"
        fi
    fi
}

# Show container logs
show_logs() {
    if ! container_exists; then
        print_error "Container ${CONTAINER} does not exist"
        exit 1
    fi
    
    $DOCKER_CMD logs -f "${CONTAINER}"
}

# Show status
show_status() {
    local status=$(get_container_status)
    
    echo "Container: ${CONTAINER}"
    echo "Status: $status"
    
    if image_exists; then
        echo "Image: exists"
    else
        echo "Image: not found"
    fi
    
    if [ "$status" = "running" ]; then
        echo "Ports: $($DOCKER_CMD port "${CONTAINER}" 2>/dev/null || echo "none")"
    fi
}

# Force rebuild
rebuild_container() {
    print_info "Rebuilding container ${CONTAINER}..."
    
    # Stop and remove if exists
    if container_exists; then
        if container_running; then
            stop_container
        fi
        $DOCKER_CMD rm "${CONTAINER}" 2>/dev/null || true
    fi
    
    # Remove image if exists
    if image_exists; then
        $DOCKER_CMD rmi "${CONTAINER}" 2>/dev/null || true
    fi
    
    # Build and start
    build_image
    start_container
}

# Cleanup on exit
cleanup() {
    if [ -f "${XAUTH}" ]; then
        rm -f "${XAUTH}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Main execution
main() {
    check_os
    check_dependencies
    check_privileges
    
    # Default behavior (no arguments)
    if [ $# -eq 0 ]; then
        if ! image_exists; then
            build_image
        fi
        start_container
        exec_shell
        exit 0
    fi
    
    # Handle commands
    case "$1" in
        start)
            if ! image_exists; then
                build_image
            fi
            start_container
            ;;
        shell)
            exec_shell
            ;;
        stop)
            stop_container
            ;;
        delete)
            delete_container
            ;;
        logs)
            show_logs
            ;;
        status)
            show_status
            ;;
        rebuild)
            rebuild_container
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            print_error "Unknown command: $1"
            usage
            ;;
    esac
}

# Run main function
main "$@"
