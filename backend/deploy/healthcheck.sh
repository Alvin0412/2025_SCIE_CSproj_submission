#!/bin/bash

echo "Checking Redis health..."
if redis-cli -h redis ping | grep -q "PONG"; then
    echo "Redis is healthy."
else
    echo "Redis is not responding!"
    exit 1
fi

echo "Checking Dramatiq health..."
if python -c "import dramatiq; dramatiq.get_worker().is_alive()" > /dev/null 2>&1; then
    echo "Dramatiq is healthy."
else
    echo "Dramatiq is not responding!"
    exit 1
fi

echo "All services are healthy!"
exit 0