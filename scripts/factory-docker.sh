#!/bin/bash
# Factory Docker helper commands

COMMAND="${1:-help}"

case "$COMMAND" in
    list)
        echo "Factory environments:"
        echo ""
        printf "%-25s %-10s %-8s %-20s\n" "CONTAINER" "TYPE" "TASK" "CREATED"
        echo "--------------------------------------------------------------------------------"
        for container in $(docker ps -q --filter "label=factory.task-id"); do
            name=$(docker inspect -f '{{.Name}}' "$container" | sed 's/\///')
            env_type=$(docker inspect -f '{{index .Config.Labels "factory.env-type"}}' "$container")
            task_id=$(docker inspect -f '{{index .Config.Labels "factory.task-id"}}' "$container")
            created=$(docker inspect -f '{{index .Config.Labels "factory.created"}}' "$container")
            created_human=$(date -d "@$created" "+%Y-%m-%d %H:%M" 2>/dev/null || echo "$created")
            printf "%-25s %-10s %-8s %-20s\n" "${name:0:25}" "$env_type" "$task_id" "$created_human"
        done
        ;;
    
    cleanup-task)
        TASK_ID="$2"
        [ -z "$TASK_ID" ] && echo "Usage: $0 cleanup-task TASK_ID" && exit 1
        echo "Cleaning up task $TASK_ID..."
        docker ps -aq --filter "label=factory.task-id=$TASK_ID" | xargs -r docker stop
        docker ps -aq --filter "label=factory.task-id=$TASK_ID" | xargs -r docker rm
        echo "Done."
        ;;
    
    cleanup-all)
        echo "Removing ALL factory environments..."
        read -p "Are you sure? (y/N) " confirm
        [ "$confirm" != "y" ] && echo "Aborted." && exit 0
        docker ps -aq --filter "label=factory.task-id" | xargs -r docker stop
        docker ps -aq --filter "label=factory.task-id" | xargs -r docker rm
        echo "Done."
        ;;
    
    logs)
        TASK_ID="$2"
        [ -z "$TASK_ID" ] && echo "Usage: $0 logs TASK_ID" && exit 1
        container=$(docker ps -q --filter "label=factory.task-id=$TASK_ID" | head -1)
        [ -z "$container" ] && echo "No container found for task $TASK_ID" && exit 1
        docker logs -f "$container"
        ;;
    
    url)
        TASK_ID="$2"
        [ -z "$TASK_ID" ] && echo "Usage: $0 url TASK_ID" && exit 1
        echo "https://task-${TASK_ID}.preview.factory.6a.fi"
        ;;
    
    *)
        echo "Factory Docker Helper"
        echo ""
        echo "Commands:"
        echo "  list              List all factory environments"
        echo "  cleanup-task ID   Remove environment for specific task"
        echo "  cleanup-all       Remove ALL factory environments"
        echo "  logs TASK_ID      Follow logs for a task's environment"
        echo "  url TASK_ID       Print preview URL for a task"
        ;;
esac
