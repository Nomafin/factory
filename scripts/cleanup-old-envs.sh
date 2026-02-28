#!/bin/bash
# Safety net: remove environments older than MAX_AGE

MAX_AGE_HOURS="${MAX_ENV_AGE_HOURS:-72}"
MAX_AGE_SECONDS=$((MAX_AGE_HOURS * 60 * 60))
NOW=$(date +%s)

echo "$(date): Cleaning up environments older than ${MAX_AGE_HOURS}h..."

cleaned=0
for container in $(docker ps -aq --filter "label=factory.task-id"); do
    created=$(docker inspect -f '{{index .Config.Labels "factory.created"}}' "$container" 2>/dev/null)
    
    if [ -n "$created" ]; then
        age=$((NOW - created))
        if [ $age -gt $MAX_AGE_SECONDS ]; then
            task_id=$(docker inspect -f '{{index .Config.Labels "factory.task-id"}}' "$container" 2>/dev/null)
            echo "Removing old environment: task-$task_id (age: $((age/3600))h)"
            docker stop "$container" >/dev/null 2>&1
            docker rm "$container" >/dev/null 2>&1
            ((cleaned++))
        fi
    fi
done

echo "$(date): Removed $cleaned old environments."
