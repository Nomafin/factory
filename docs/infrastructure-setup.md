# Factory Infrastructure Setup

This guide covers setting up the infrastructure for Factory's Docker preview environments, including Traefik reverse proxy, nginx, SSL certificates, and cleanup automation.

## Prerequisites

- Ubuntu/Debian server with root access
- Docker installed and running
- nginx installed
- A domain with wildcard DNS (e.g., `*.preview.factory.example.com`)
- Ports 80 and 443 available (nginx) or alternate ports for Traefik

## Architecture Overview

```
Internet
    │
    ▼
┌─────────┐     ┌─────────┐     ┌──────────────────┐
│  nginx  │────▶│ Traefik │────▶│ Preview Containers│
│ :80/:443│     │  :8180  │     │ (auto-discovered) │
└─────────┘     └─────────┘     └──────────────────┘
    │
    └── SSL termination (Let's Encrypt)
```

- **nginx**: Handles SSL termination, proxies to Traefik
- **Traefik**: Auto-discovers Docker containers, routes by hostname
- **Preview containers**: Labelled with Traefik rules for routing

## Step 1: DNS Configuration

Set up a wildcard DNS record pointing to your server:

```
*.preview.factory.example.com → YOUR_SERVER_IP
```

Verify with:
```bash
dig +short test.preview.factory.example.com
# Should return your server IP
```

## Step 2: Create Docker Network

```bash
docker network create factory-preview
```

## Step 3: Set Up Traefik

Create the Traefik directory and configuration:

```bash
mkdir -p /opt/factory/traefik/certs
```

Create `/opt/factory/traefik/traefik.yml`:

```yaml
api:
  dashboard: true
  insecure: true  # Dashboard on :8181

entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"
    http:
      tls: {}

providers:
  docker:
    endpoint: "unix:///var/run/docker.sock"
    exposedByDefault: false
    network: factory-preview
    watch: true

tls:
  stores:
    default:
      defaultCertificate:
        certFile: /certs/cert.pem
        keyFile: /certs/key.pem

log:
  level: INFO

accessLog: {}
```

Create `/opt/factory/traefik/docker-compose.yml`:

```yaml
services:
  traefik:
    image: traefik:latest
    container_name: traefik
    restart: always
    ports:
      - "8180:80"      # HTTP (behind nginx)
      - "8443:443"     # HTTPS (behind nginx)
      - "8181:8080"    # Dashboard
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik.yml:/etc/traefik/traefik.yml:ro
      - ./certs:/certs:ro
    networks:
      - factory-preview

networks:
  factory-preview:
    external: true
```

Start Traefik:

```bash
cd /opt/factory/traefik
docker compose up -d
```

## Step 4: Configure nginx

Create `/etc/nginx/sites-available/preview.factory.example.com`:

```nginx
# Factory Preview Environments
# Proxies *.preview.factory.example.com to Traefik

server {
    listen 80;
    listen [::]:80;
    server_name *.preview.factory.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name *.preview.factory.example.com;

    # SSL certificates (see Step 5)
    ssl_certificate /etc/letsencrypt/live/preview.factory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/preview.factory.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # For SSE/long-polling
        proxy_buffering off;
        proxy_read_timeout 86400s;
    }

    access_log /var/log/nginx/preview.factory.access.log;
    error_log /var/log/nginx/preview.factory.error.log;
}
```

Enable the site:

```bash
ln -sf /etc/nginx/sites-available/preview.factory.example.com /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## Step 5: SSL Certificates

### Option A: Let's Encrypt Wildcard (Recommended)

For wildcard certificates, you need DNS validation:

```bash
certbot certonly --manual --preferred-challenges dns \
  -d "*.preview.factory.example.com"
```

When prompted:
1. Add the TXT record to your DNS
2. Wait for propagation (verify with `dig TXT _acme-challenge.preview.factory.example.com`)
3. Press Enter to continue

**Note:** Wildcard certs require manual renewal every 90 days. Set a reminder!

### Option B: Self-Signed (Testing Only)

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /opt/factory/traefik/certs/key.pem \
  -out /opt/factory/traefik/certs/cert.pem \
  -subj "/CN=*.preview.factory.example.com"
```

Update nginx to use these certs instead of Let's Encrypt paths.

## Step 6: Cleanup Automation

Create cleanup scripts for managing preview environments.

### Cleanup Merged PRs

Create `/opt/factory/scripts/cleanup-merged-prs.sh`:

```bash
#!/bin/bash
# Removes preview containers for merged/closed PRs

set -euo pipefail

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
REPO="${1:-}"

if [[ -z "$GITHUB_TOKEN" || -z "$REPO" ]]; then
    echo "Usage: GITHUB_TOKEN=xxx $0 owner/repo"
    exit 1
fi

# Get preview containers
containers=$(docker ps -q --filter "label=factory.env-type=preview" --filter "label=factory.repo=$REPO")

for container in $containers; do
    pr_number=$(docker inspect --format '{{ index .Config.Labels "factory.pr-number" }}' "$container" 2>/dev/null || echo "")
    
    if [[ -n "$pr_number" ]]; then
        # Check if PR is still open
        state=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
            "https://api.github.com/repos/$REPO/pulls/$pr_number" | jq -r '.state')
        
        if [[ "$state" != "open" ]]; then
            echo "Removing container for closed PR #$pr_number"
            docker stop "$container" && docker rm "$container"
        fi
    fi
done
```

### Cleanup Old Environments

Create `/opt/factory/scripts/cleanup-old-envs.sh`:

```bash
#!/bin/bash
# Removes environments older than specified hours

MAX_AGE_HOURS="${1:-72}"
MAX_AGE_SECONDS=$((MAX_AGE_HOURS * 3600))
NOW=$(date +%s)

containers=$(docker ps -aq --filter "label=factory.task-id")

for container in $containers; do
    created=$(docker inspect --format '{{ index .Config.Labels "factory.created" }}' "$container" 2>/dev/null || echo "0")
    
    if [[ -n "$created" && "$created" != "0" ]]; then
        age=$((NOW - created))
        if [[ $age -gt $MAX_AGE_SECONDS ]]; then
            task_id=$(docker inspect --format '{{ index .Config.Labels "factory.task-id" }}' "$container")
            echo "Removing old container for task $task_id (age: $((age/3600))h)"
            docker stop "$container" 2>/dev/null
            docker rm "$container" 2>/dev/null
        fi
    fi
done

# Prune dangling images
docker image prune -f
```

Make scripts executable:

```bash
chmod +x /opt/factory/scripts/*.sh
```

### Cron Jobs

Create `/etc/cron.d/factory-cleanup`:

```cron
# Factory preview environment cleanup
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Cleanup merged PRs every hour
0 * * * * root GITHUB_TOKEN="your-token" /opt/factory/scripts/cleanup-merged-prs.sh owner/repo >> /var/log/factory-cleanup.log 2>&1

# Cleanup old environments daily at 4 AM
0 4 * * * root /opt/factory/scripts/cleanup-old-envs.sh 72 >> /var/log/factory-cleanup.log 2>&1
```

## Step 7: Verify Setup

Test the complete flow:

```bash
# 1. Create a test container
docker run -d --name test-preview \
  --network factory-preview \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.test.rule=Host(\`test.preview.factory.example.com\`)" \
  --label "traefik.http.routers.test.entrypoints=websecure" \
  --label "traefik.http.routers.test.tls=true" \
  --label "traefik.http.services.test.loadbalancer.server.port=80" \
  nginx:alpine

# 2. Test the URL
curl -I https://test.preview.factory.example.com/

# 3. Cleanup
docker stop test-preview && docker rm test-preview
```

## Troubleshooting

### Container not accessible

1. Check container is on `factory-preview` network:
   ```bash
   docker network inspect factory-preview
   ```

2. Check Traefik detected the container:
   ```bash
   curl http://localhost:8181/api/http/routers
   ```

3. Check nginx is proxying correctly:
   ```bash
   tail -f /var/log/nginx/preview.factory.error.log
   ```

### SSL certificate errors

1. Verify certificate is valid:
   ```bash
   openssl s_client -connect preview.factory.example.com:443 -servername test.preview.factory.example.com
   ```

2. Check nginx config:
   ```bash
   nginx -t
   ```

### Traefik not discovering containers

1. Check Docker socket permissions
2. Verify container has `traefik.enable=true` label
3. Check container is on `factory-preview` network

## Quick Reference

| Component | Port | Purpose |
|-----------|------|---------|
| nginx | 80, 443 | SSL termination, public entry |
| Traefik HTTP | 8180 | Container routing (internal) |
| Traefik HTTPS | 8443 | Container routing (internal) |
| Traefik Dashboard | 8181 | Admin UI |

| File | Purpose |
|------|---------|
| `/opt/factory/traefik/traefik.yml` | Traefik configuration |
| `/opt/factory/traefik/docker-compose.yml` | Traefik container |
| `/etc/nginx/sites-available/preview.*` | nginx proxy config |
| `/opt/factory/scripts/cleanup-*.sh` | Cleanup scripts |
| `/etc/cron.d/factory-cleanup` | Scheduled cleanup |
