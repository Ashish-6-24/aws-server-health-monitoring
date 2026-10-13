
import json
import logging
import re
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = "us-east-2"
DESTINATION_TOPIC_ARN = (
    "arn:aws:sns:us-east-2:941141114260:"
    "server-health-formatted-notifications"
)

s3 = boto3.client("s3", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)


def read_value(report, labels, default="Not reported"):
    """Find a value following one of the supplied labels."""
    for line in report.splitlines():
        for label in labels:
            match = re.match(
                r"^\s*" + re.escape(label) + r"\s*:\s*(.*?)\s*$",
                line,
                re.IGNORECASE,
            )
            if match and match.group(1):
                return match.group(1)
    return default


def numeric_value(value):
    """Extract a number from values such as '91.2%' or '1.4 GiB'."""
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def assess_metric(value, threshold):
    """Determine status only when both measurements are available."""
    actual = numeric_value(value)
    limit = numeric_value(threshold)

    if actual is None or limit is None:
        return "NOT VERIFIED"

    return "ALERT" if actual >= limit else "NORMAL"


def lambda_handler(event, context):
    logger.info("Received notification event")
    processed = 0

    for outer_record in event.get("Records", []):
        sns_record = outer_record.get("Sns")
        if not sns_record:
            continue

        try:
            s3_event = json.loads(sns_record["Message"])
        except (KeyError, json.JSONDecodeError):
            logger.exception("Invalid SNS message")
            raise

        # Ignore S3's notification-configuration test event.
        if s3_event.get("Event") == "s3:TestEvent":
            continue

        for record in s3_event.get("Records", []):
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(record["s3"]["object"]["key"])

            if not key.startswith("logs/") or not key.endswith(".txt"):
                continue

            response = s3.get_object(Bucket=bucket, Key=key)
            report = response["Body"].read().decode(
                "utf-8", errors="replace"
            ).strip()

            host = read_value(report, ["Host"])
            timestamp = read_value(
                report, ["Timestamp", "Time"]
            )
            event_name = record.get("eventName", "Unknown")

            # CPU measurements
            cpu = read_value(report, [
                "CPU Utilization",
                "Observed Aggregate CPU Utilization",
                "CPU Usage",
                "CPU Usage Percent",
            ])
            cpu_threshold = read_value(report, [
                "CPU Threshold",
                "CPU Alert Threshold",
            ])

            # Memory measurements
            memory = read_value(report, [
                "Memory Usage",
                "Observed Memory Usage",
                "Memory Utilization",
                "Memory Usage Percent",
            ])
            memory_threshold = read_value(report, [
                "Memory Threshold",
                "Memory Alert Threshold",
            ])
            memory_used = read_value(report, ["Memory Used"])
            memory_total = read_value(report, ["Memory Total"])

            # Service measurements
            service_name = read_value(report, [
                "Service Name", "Test Service"
            ])
            service_status = read_value(report, [
                "Service Status", "Observed Service Status",
                "Observed systemd state",
            ])
            expected_status = read_value(
                report, ["Expected Service Status"], "active"
            )

            cpu_status = assess_metric(cpu, cpu_threshold)
            memory_status = assess_metric(
                memory, memory_threshold
            )

            is_test = (
                "TEST ALERT ONLY" in report.upper()
                or "TEST REPORT" in report.upper()
                or "SIMULATED" in report.upper()
            )

            # Only claim a service failure if the observed status
            # differs from the expected status.
            service_failed = (
                service_status.lower() in
                {"failed", "inactive", "dead", "not running"}
            )
            service_status_label = (
                "ALERT" if service_failed else
                "NORMAL" if service_status.lower() == expected_status.lower()
                else "CHECK"
            )

            if is_test:
                subject_prefix = "[TEST]"
                overall_status = "TEST / SIMULATED INCIDENT"
            elif "ALERT" in (
                cpu_status + memory_status + service_status_label
            ):
                subject_prefix = "[ALERT]"
                overall_status = "HEALTH ALERT"
            elif "NOT VERIFIED" in (cpu_status + memory_status):
                subject_prefix = "[CHECK]"
                overall_status = "INCOMPLETE METRICS"
            else:
                subject_prefix = "[INFO]"
                overall_status = "HEALTH REPORT"

            subject = f"{subject_prefix} Server Health - {host}"[:100]

            message = f"""
SERVER HEALTH MONITORING REPORT
==================================================

OVERALL STATUS : {overall_status}
SERVER         : {host}
TIMESTAMP      : {timestamp}
AWS REGION     : {AWS_REGION}

1. CPU METRICS
--------------------------------------------------
Measured CPU utilization : {cpu}
Alert threshold          : {cpu_threshold}
Assessment               : {cpu_status}

2. MEMORY METRICS
--------------------------------------------------
Measured memory usage    : {memory}
Alert threshold          : {memory_threshold}
Memory used              : {memory_used}
Total memory             : {memory_total}
Assessment               : {memory_status}

3. SERVICE STATUS
--------------------------------------------------
Service name             : {service_name}
Observed status          : {service_status}
Expected status          : {expected_status}
Assessment               : {service_status_label}

4. REPORT DETAILS
--------------------------------------------------
S3 bucket                : {bucket}
S3 object                : {key}
S3 event                 : {event_name}
S3 URI                   : s3://{bucket}/{key}

SOURCE REPORT
--------------------------------------------------
{report}

==================================================
Generated by the Server Health Monitoring pipeline.
Values are taken from the uploaded report.
Missing measurements are not inferred.
""".strip()

            result = sns.publish(
                TopicArn=DESTINATION_TOPIC_ARN,
                Subject=subject,
                Message=message,
            )

            logger.info(
                "Formatted notification published: %s",
                result["MessageId"],
            )
            processed += 1

    logger.info("Reports processed: %s", processed)

    return {
        "statusCode": 200,
        "body": json.dumps({"reports_processed": processed}),
    }

