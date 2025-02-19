#!/bin/bash

# Function to display script usage in a clear format
show_usage() {
    echo "Redis Server Manager"
    echo "Usage: ./redis_manager.sh [command]"
    echo ""
    echo "Commands:"
    echo "  start    - Start Redis server"
    echo "  stop     - Stop Redis server"
    echo "  status   - Check Redis server status"
    echo "  restart  - Restart Redis server"
}

# Function to check if Redis is already running
check_redis_running() {
    if pgrep redis-server > /dev/null; then
        return 0  # Redis is running
    else
        return 1  # Redis is not running
    fi
}

# Function to start Redis
start_redis() {
    echo "Attempting to start Redis server..."
    if check_redis_running; then
        echo "Redis is already running!"
    else
        # Try to start Redis using different methods depending on the system
        if command -v systemctl > /dev/null; then
            sudo systemctl start redis-server
        elif command -v service > /dev/null; then
            sudo service redis-server start
        else
            echo "Error: Could not detect system service manager"
            exit 1
        fi

        # Verify that Redis started successfully
        sleep 2
        if check_redis_running; then
            echo "Redis server started successfully!"
        else
            echo "Failed to start Redis server. Please check system logs."
            exit 1
        fi
    fi
}

# Function to stop Redis
stop_redis() {
    echo "Attempting to stop Redis server..."
    if ! check_redis_running; then
        echo "Redis is not running!"
    else
        if command -v systemctl > /dev/null; then
            redis-cli FLUSHALL
            sudo systemctl stop redis
            sudo systemctl stop redis-server
        elif command -v service > /dev/null; then
            sudo service redis-server stop
        else
            echo "Error: Could not detect system service manager"
            exit 1
        fi

        # Verify that Redis stopped successfully
        sleep 2
        if ! check_redis_running; then
            echo "Redis server stopped successfully!"
        else
            echo "Failed to stop Redis server. Please check system logs."
            exit 1
        fi
    fi
}

# Function to check Redis status
check_status() {
    echo "Checking Redis server status..."
    if check_redis_running; then
        echo "Redis server is running"
        redis-cli ping > /dev/null 2>&1
        if [ $? -eq 0 ]; then
            echo "Redis is responding to connections"
        else
            echo "Warning: Redis is running but not responding to connections"
        fi
    else
        echo "Redis server is not running"
    fi
}

# Main script logic
case "$1" in
    start)
        start_redis
        ;;
    stop)
        stop_redis
        ;;
    status)
        check_status
        ;;
    restart)
        stop_redis
        sleep 2
        start_redis
        ;;
    *)
        show_usage
        exit 1
        ;;
esac

exit 0
