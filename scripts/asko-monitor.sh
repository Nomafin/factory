#!/bin/bash
# Monitor Asko ROLLY coffee table for price drops
# Product: ROLLY-sohvapöytä 60 x 90 cm (saarni)
# Target: Alert when price drops below €199.60

PRODUCT_URL="https://www.asko.fi/tuotteet/tuote/70/34693/rolly-sohvapoyta-60-x-90-cm-saarni"
STATE_FILE="/opt/factory/scripts/asko-state.json"
LOG_FILE="/var/log/asko-monitor.log"
BASELINE_PRICE="199.60"

# Fetch the page and extract price from JSON-LD
PAGE=$(curl -s "$PRODUCT_URL" 2>/dev/null)
PRICE=$(echo "$PAGE" | grep -oP '"price"\s*:\s*"\K[0-9.]+' | head -1)

if [ -z "$PRICE" ]; then
    echo "$(date): ERROR - Could not fetch price" >> "$LOG_FILE"
    exit 1
fi

# Load previous state
if [ -f "$STATE_FILE" ]; then
    PREV_PRICE=$(jq -r '.price // "unknown"' "$STATE_FILE" 2>/dev/null)
else
    PREV_PRICE="unknown"
fi

# Save current state
cat > "$STATE_FILE" << EOF
{
  "price": "$PRICE",
  "baseline": "$BASELINE_PRICE",
  "product": "ROLLY-sohvapöytä 60x90cm saarni",
  "url": "$PRODUCT_URL",
  "last_checked": "$(date -Iseconds)"
}
EOF

# Check for price drop
if [ "$PRICE" != "$PREV_PRICE" ]; then
    # Price changed
    if [ "$(echo "$PRICE < $BASELINE_PRICE" | bc -l)" = "1" ]; then
        # Price dropped below baseline!
        MSG="🎉 *ASKO Price Drop!*

ROLLY-sohvapöytä 60x90cm (saarni)
Was: €$BASELINE_PRICE
Now: *€$PRICE*

⚠️ Check Tampere Hakametsä stock!

$PRODUCT_URL"
        
        echo "$(date): ALERT - Price dropped to €$PRICE (was €$PREV_PRICE, baseline €$BASELINE_PRICE)" >> "$LOG_FILE"
        
        # Send Telegram notification via OpenClaw
        curl -s -X POST "http://localhost:3033/api/send" \
            -H "Content-Type: application/json" \
            -d "{\"message\": \"$MSG\", \"target\": \"5385652970\"}" > /dev/null
    else
        echo "$(date): Price changed €$PREV_PRICE → €$PRICE (baseline €$BASELINE_PRICE)" >> "$LOG_FILE"
    fi
else
    echo "$(date): OK - Price €$PRICE (unchanged)" >> "$LOG_FILE"
fi
