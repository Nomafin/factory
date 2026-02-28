#!/bin/bash
# Cleanup environments for merged/closed PRs

echo "$(date): Starting cleanup of merged/closed PR environments..."

cleaned=0
for container in $(docker ps -aq --filter "label=factory.env-type=preview"); do
    pr_number=$(docker inspect -f '{{index .Config.Labels "factory.pr-number"}}' "$container" 2>/dev/null)
    repo=$(docker inspect -f '{{index .Config.Labels "factory.repo"}}' "$container" 2>/dev/null)
    
    if [ -n "$pr_number" ] && [ -n "$repo" ]; then
        state=$(gh pr view "$pr_number" --repo "$repo" --json state -q '.state' 2>/dev/null)
        
        if [ "$state" = "MERGED" ] || [ "$state" = "CLOSED" ]; then
            task_id=$(docker inspect -f '{{index .Config.Labels "factory.task-id"}}' "$container" 2>/dev/null)
            echo "Cleaning up: task-$task_id / PR #$pr_number ($state)"
            docker stop "$container" >/dev/null 2>&1
            docker rm "$container" >/dev/null 2>&1
            ((cleaned++))
        fi
    fi
done

docker volume prune -f --filter "label=factory.task-id" >/dev/null 2>&1
docker network prune -f --filter "label=factory.task-id" >/dev/null 2>&1

echo "$(date): Cleanup complete. Removed $cleaned environments."
