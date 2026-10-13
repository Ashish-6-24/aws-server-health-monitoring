#!/bin/bash

set -euo pipefail

# Configuration & Thresholds
DISK_THRESHOLD=80
MEM_THRESHOLD=80
LOG_FILE="/var/log/server_health.log"

AWS_REGION="us-east-2"
S3_BUCKET="ashish-server-health-logs"
S3_PREFIX="logs/"

ALERT_COOLDOWN=1800
ALERT_STATE_FILE="/var/tmp/server_health_alert_last_sent"

SERVICES=("ssh")
ALERTS=()

# Function to write messages to the log file
log_message() {
    local level="$1"
    local message="$2"

    echo "$(date '+%Y-%m-%d %H:%M:%S') [$level]: $message" >> "$LOG_FILE"
}

# Function to collect alert messages
send_alert() {
    local message="$1"

    log_message "ALERT" "$message"
    ALERTS+=("$message")
}

# Check required commands
for cmd in df free awk tr systemctl date hostname aws mktemp touch rm; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: Required command '$cmd' is missing." >&2
        exit 2
    fi
done

# Check AWS configuration
if [[ -z "$AWS_REGION" || "$AWS_REGION" == "YOUR_AWS_REGION" ||
      -z "$S3_BUCKET" || "$S3_BUCKET" == "YOUR_S3_BUCKET_NAME" ]]; then
    echo "ERROR: Configure AWS_REGION and S3_BUCKET first." >&2
    exit 2
fi

# Make sure the log file is writable
if ! touch "$LOG_FILE" 2>/dev/null; then
    echo "ERROR: Cannot write to $LOG_FILE. Check file permissions." >&2
    exit 2
fi

# 1. Check Disk Usage (Root Partition)
DISK_USAGE=$(df -P / | awk 'NR==2 {print $5}' | tr -d '%') || DISK_USAGE=""

if [[ "$DISK_USAGE" =~ ^[0-9]+$ ]]; then
    if [ "$DISK_USAGE" -ge "$DISK_THRESHOLD" ]; then
        send_alert "Root disk usage is high: ${DISK_USAGE}%"
    else
        log_message "INFO" "Disk usage is normal (${DISK_USAGE}%)."
    fi
else
    send_alert "Could not read root disk usage."
fi

# 2. Check Memory Usage
MEM_USAGE=$(free | awk '/^Mem:/ {
    if ($2 > 0 && $7 ~ /^[0-9]+$/)
        printf "%d", (($2 - $7) * 100 / $2)
}') || MEM_USAGE=""

if [[ "$MEM_USAGE" =~ ^[0-9]+$ ]]; then
    if [ "$MEM_USAGE" -ge "$MEM_THRESHOLD" ]; then
        send_alert "Memory usage is high: ${MEM_USAGE}%"
    else
        log_message "INFO" "Memory usage is normal (${MEM_USAGE}%)."
    fi
else
    send_alert "Could not read memory usage."
fi

# 3. Check Running Services
for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$service"; then
        log_message "INFO" "Service '$service' is running."
    else
        send_alert "Service '$service' is DOWN!"
    fi
done

# 4. Upload an Alert Report to S3
if [ "${#ALERTS[@]}" -gt 0 ]; then

    CURRENT_TIME=$(date +%s)
    LAST_ALERT_TIME=0

    # Read the time of the previous successful alert upload
    if [ -r "$ALERT_STATE_FILE" ]; then
        read -r LAST_ALERT_TIME < "$ALERT_STATE_FILE" || true
    fi

    # Prevent repeated uploads during the 30-minute cooldown
    if [[ "$LAST_ALERT_TIME" =~ ^[0-9]+$ ]] &&
       [ "$LAST_ALERT_TIME" -gt 0 ] &&
       [ "$CURRENT_TIME" -ge "$LAST_ALERT_TIME" ] &&
       [ "$((CURRENT_TIME - LAST_ALERT_TIME))" -lt "$ALERT_COOLDOWN" ]; then

        log_message "INFO" "Alert cooldown is active. Skipping upload."
        exit 1
    fi

    # Create a temporary report file
    if ! REPORT_FILE=$(mktemp /tmp/server-health-alert.XXXXXX); then
        log_message "ERROR" "Could not create alert report."
        exit 1
    fi

    # Prepare a unique S3 report name
    HOST=$(hostname)
    TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
    S3_KEY="${S3_PREFIX}health-alert-${HOST}-${TIMESTAMP}-$$.txt"

    # Write alert details into the report
    {
        echo "Server Health Alert"
        echo "Host: $HOST"
        echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "S3 Object: s3://${S3_BUCKET}/${S3_KEY}"
        echo ""
        echo "Alert Details:"

        for alert in "${ALERTS[@]}"; do
            echo "- $alert"
        done
    } > "$REPORT_FILE"

    # Upload the report to S3
    if aws s3 cp "$REPORT_FILE" "s3://${S3_BUCKET}/${S3_KEY}" \
        --region "$AWS_REGION" --only-show-errors; then

        log_message "INFO" "Alert report uploaded to S3: ${S3_KEY}"

        # Save the time of the successful upload
        if ! printf '%s\n' "$CURRENT_TIME" > "$ALERT_STATE_FILE"; then
            log_message "WARN" "Could not save alert cooldown state."
        fi

    else
        log_message "ERROR" "S3 upload failed. Check IAM permissions and bucket settings."
        rm -f "$REPORT_FILE"
        exit 1
    fi

    # Remove the temporary report
    rm -r "$REPORT_FILE"

    log_message "ERROR" "Overall status: ALERT."
    exit 1

else
    # Clear the previous alert time after recovery
    log_message "INFO" "All health checks passed. Server is HEALTHY."
    rm -r "$ALERT_STATE_FILE"
    exit 0
fi

