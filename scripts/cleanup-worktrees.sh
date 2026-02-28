#!/bin/bash
# Cleanup orphaned worktrees and branches from failed/cancelled tasks.
#
# Removes worktree directories older than MAX_AGE that match the agent branch
# naming convention (agent/task-*), prunes stale git worktree references, and
# deletes corresponding local branches.
#
# Usage:
#   cleanup-worktrees.sh                    # default: 24h max age
#   MAX_WORKTREE_AGE_HOURS=48 cleanup-worktrees.sh
#
# Designed to run as a cron job alongside cleanup-old-envs.sh.

set -euo pipefail

FACTORY_ROOT="${FACTORY_ROOT:-/opt/factory}"
WORKTREES_DIR="${FACTORY_ROOT}/worktrees"
REPOS_DIR="${FACTORY_ROOT}/repos"
MAX_AGE_HOURS="${MAX_WORKTREE_AGE_HOURS:-24}"
MAX_AGE_MINUTES=$((MAX_AGE_HOURS * 60))

echo "$(date): Cleaning up worktrees older than ${MAX_AGE_HOURS}h..."

cleaned_wt=0
cleaned_br=0

# Safety: skip if worktrees dir doesn't exist
if [ ! -d "$WORKTREES_DIR" ]; then
    echo "$(date): Worktrees directory $WORKTREES_DIR does not exist, nothing to do."
    exit 0
fi

# Find worktree directories matching the agent naming convention that are
# older than MAX_AGE_MINUTES.  The -maxdepth 1 ensures we only look at
# top-level entries (each worktree is a single directory).
for wt_dir in $(find "$WORKTREES_DIR" -maxdepth 1 -mindepth 1 -type d -name "agent-task-*" -mmin "+${MAX_AGE_MINUTES}" 2>/dev/null); do
    wt_name=$(basename "$wt_dir")
    # Convert slug back to branch name (agent-task-N-slug -> agent/task-N-slug)
    branch_name=$(echo "$wt_name" | sed 's/^agent-/agent\//')

    echo "Removing orphaned worktree: $wt_name (branch: $branch_name)"

    # Try to find which repo this worktree belongs to by checking each repo
    removed=false
    for repo_dir in "$REPOS_DIR"/*/; do
        [ -d "$repo_dir/.git" ] || continue

        # Try git worktree remove first
        if git -C "$repo_dir" worktree remove "$wt_dir" --force 2>/dev/null; then
            removed=true
        fi

        # Prune stale references
        git -C "$repo_dir" worktree prune 2>/dev/null || true

        # Delete the local branch if it exists
        if git -C "$repo_dir" branch -D "$branch_name" 2>/dev/null; then
            echo "  Deleted local branch: $branch_name"
            ((cleaned_br++))
        fi

        # Delete remote branch (best-effort, only for branches without PRs)
        # We skip remote deletion here since the orchestrator handles it
        # during task failure. This script only cleans up local resources.

        if [ "$removed" = true ]; then
            break
        fi
    done

    # Fallback: remove directory manually if git didn't handle it
    if [ -d "$wt_dir" ]; then
        rm -rf "$wt_dir"
        echo "  Removed directory: $wt_dir"
    fi

    ((cleaned_wt++))
done

# Prune stale worktree references in all repos
for repo_dir in "$REPOS_DIR"/*/; do
    [ -d "$repo_dir/.git" ] || continue
    git -C "$repo_dir" worktree prune 2>/dev/null || true
done

echo "$(date): Cleanup complete. Removed $cleaned_wt worktree(s), $cleaned_br branch(es)."
