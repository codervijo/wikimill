#!/bin/bash

CONTAINER=lscrape3
HOST_PORT=6000
CONTAINER_PORT=3445
VOLUME_MOUNT="${PWD}:/usr/src/app/"

# Usage
usage() {
    echo "Usage: $0 {start|shell|stop|delete}"
    exit 1
}

# Build image
build_image() {
    echo "Building image..."
    sudo docker build --tag ${CONTAINER}:latest --cache-from ${CONTAINER}:latest -t ${CONTAINER} . || \
    sudo docker build -t ${CONTAINER} .
}

# Check if container exists
container_exists() {
    sudo docker ps -aq -f name="^${CONTAINER}$" | grep -q .
}

# Check if container is running
container_running() {
    sudo docker ps -q -f name="^${CONTAINER}$" | grep -q .
}

# Start container
start_container() {
    if container_running; then
        echo "Container ${CONTAINER} is already running"
    else
        echo "Starting container..."
        sudo docker run -d -v "${VOLUME_MOUNT}" --network=host -p ${HOST_PORT}:${CONTAINER_PORT} \
            --name ${CONTAINER} ${CONTAINER} tail -f /dev/null
        echo "Container ${CONTAINER} started"
    fi
}

# Main logic
if [ $# -eq 0 ]; then
    build_image
    start_container
    sudo docker exec -it ${CONTAINER} /bin/bash
    exit 0
fi

case "$1" in
    start)
        build_image
        start_container
        ;;
    shell)
        if container_running; then
            sudo docker exec -it ${CONTAINER} /bin/bash
        else
            echo "Container ${CONTAINER} is not running"
            exit 1
        fi
        ;;
    stop)
        if container_running; then
            sudo docker stop ${CONTAINER}
            echo "Container ${CONTAINER} stopped"
        else
            echo "Container ${CONTAINER} is not running"
            exit 1
        fi
        ;;
    delete)
        if container_exists; then
            if container_running; then
                sudo docker stop ${CONTAINER}
            fi
            sudo docker rm ${CONTAINER}
            sudo docker rmi ${CONTAINER}
            echo "Container ${CONTAINER} deleted"
        else
            echo "Container ${CONTAINER} does not exist"
            exit 1
        fi
        ;;
    *)
        usage
        ;;
esac
