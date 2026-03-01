#!/bin/bash
# Factory nginx setup script
# Usage: ./setup-nginx.sh [domain] [htpasswd_user]
# Example: ./setup-nginx.sh factory.6a.fi admin

set -e

DOMAIN="${1:-factory.example.com}"
HTPASSWD_USER="${2:-admin}"
HTPASSWD_FILE="/etc/nginx/.htpasswd_factory"
NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"
FACTORY_PORT="${FACTORY_PORT:-8100}"
TRAEFIK_PORT="${TRAEFIK_PORT:-8180}"

echo "Setting up nginx for Factory at ${DOMAIN}"

# Check if nginx is installed
if ! command -v nginx &> /dev/null; then
    echo "Error: nginx is not installed"
    exit 1
fi

# Create htpasswd file if it doesn't exist
if [ ! -f "$HTPASSWD_FILE" ]; then
    echo "Creating basic auth file..."
    if command -v htpasswd &> /dev/null; then
        echo "Enter password for user '${HTPASSWD_USER}':"
        htpasswd -c "$HTPASSWD_FILE" "$HTPASSWD_USER"
    else
        echo "Error: htpasswd not found. Install apache2-utils: apt install apache2-utils"
        exit 1
    fi
else
    echo "Using existing htpasswd file: $HTPASSWD_FILE"
fi

# Create nginx config
cat > "$NGINX_CONF" << EOF
# Factory Agent Farm - Main Site
server {
    server_name ${DOMAIN};

    # Basic auth for all endpoints
    auth_basic "Factory Agent Farm";
    auth_basic_user_file ${HTPASSWD_FILE};

    # Static files (CSS, JS)
    location /static/ {
        proxy_pass http://localhost:${FACTORY_PORT}/static/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Dashboard UI
    location /dashboard {
        proxy_pass http://localhost:${FACTORY_PORT}/dashboard;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Auth endpoints (OAuth flow)
    location /auth/ {
        proxy_pass http://localhost:${FACTORY_PORT}/auth/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # SSE needs special handling - longer timeouts, no buffering
    location /api/messages/stream/sse {
        proxy_pass http://localhost:${FACTORY_PORT}/api/messages/stream/sse;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
        chunked_transfer_encoding off;
    }

    # API endpoints
    location /api/ {
        proxy_pass http://localhost:${FACTORY_PORT}/api/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Message board UI
    location /messages {
        proxy_pass http://localhost:${FACTORY_PORT}/messages;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Root redirect to dashboard
    location = / {
        return 302 /dashboard;
    }

    # Health check (no auth needed for monitoring)
    location /health {
        auth_basic off;
        proxy_pass http://localhost:${FACTORY_PORT}/health;
    }

    listen 80;
}

# Factory Preview Environments
# Proxies *.preview.${DOMAIN} to Traefik for Docker auto-discovery
server {
    listen 80;
    server_name *.preview.${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${TRAEFIK_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # For SSE/long-polling
        proxy_buffering off;
        proxy_read_timeout 86400s;
    }

    access_log /var/log/nginx/preview.${DOMAIN}.access.log;
    error_log /var/log/nginx/preview.${DOMAIN}.error.log;
}
EOF

# Enable site
ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${DOMAIN}"

# Test config
echo "Testing nginx configuration..."
nginx -t

# Reload nginx
echo "Reloading nginx..."
systemctl reload nginx

echo ""
echo "✅ nginx configured for ${DOMAIN}"
echo ""
echo "Next steps:"
echo "1. Point DNS for ${DOMAIN} and *.preview.${DOMAIN} to this server"
echo "2. Get SSL certificate:"
echo "   certbot --nginx -d ${DOMAIN}"
echo "   certbot certonly --manual --preferred-challenges dns -d \"*.preview.${DOMAIN}\""
echo ""
echo "Dashboard: http://${DOMAIN}/dashboard"
echo "API: http://${DOMAIN}/api/"
