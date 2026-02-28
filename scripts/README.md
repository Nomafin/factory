# Factory Scripts

Utility scripts for managing Factory and preview environments.

## Cleanup Scripts

### cleanup-merged-prs.sh

Removes Docker preview containers for PRs that have been merged or closed.

```bash
GITHUB_TOKEN=ghp_xxx ./cleanup-merged-prs.sh owner/repo
```

**Recommended:** Run hourly via cron.

### cleanup-old-envs.sh

Removes Docker containers older than a specified age (default: 72 hours).

```bash
./cleanup-old-envs.sh [max_age_hours]
./cleanup-old-envs.sh 48  # Remove containers older than 48 hours
```

**Recommended:** Run daily via cron as a safety net.

### factory-docker.sh

Helper commands for managing Factory Docker environments.

```bash
./factory-docker.sh list              # List all Factory containers
./factory-docker.sh cleanup-task 42   # Remove containers for task 42
./factory-docker.sh logs 42           # View logs for task 42
./factory-docker.sh url 42            # Get preview URL for task 42
```

## Cron Setup

Add to `/etc/cron.d/factory-cleanup`:

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Cleanup merged PRs every hour
0 * * * * root GITHUB_TOKEN="xxx" /opt/factory/scripts/cleanup-merged-prs.sh owner/repo >> /var/log/factory-cleanup.log 2>&1

# Cleanup old environments daily at 4 AM
0 4 * * * root /opt/factory/scripts/cleanup-old-envs.sh 72 >> /var/log/factory-cleanup.log 2>&1
```

## See Also

- [Infrastructure Setup Guide](../docs/infrastructure-setup.md) — Complete setup instructions
- [AGENTS.md](../AGENTS.md) — Agent guide including Docker environments
