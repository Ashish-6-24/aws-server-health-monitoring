# Server Health Monitoring and Alerting — Deployment Guide

## Overview

This runbook documents how to deploy the Bash-based Linux health check and its AWS event-driven notification pipeline. It covers EC2 scheduling, S3 report storage, SNS routing, Lambda email formatting, IAM permissions, verification, and cleanup.

Follow the stages in order. Do not mark the pipeline complete until the final test produces a readable email in the intended inbox.

## 1. Prerequisites

- An AWS account with permission to configure EC2, IAM, S3, SNS, Lambda, and CloudWatch Logs.
- A running Ubuntu EC2 instance with `bash`, `python3`, `aws`, `cron`, and `flock` available as required by the scripts.
- An EC2 instance profile attached to the instance. Do not place long-lived access keys in the script or repository.
- An S3 bucket named `ashish-server-health-logs` in `us-east-2` (or update the project configuration consistently if using another bucket).
- Access to confirm an SNS email subscription.
- The working script at `/usr/local/bin/server_health_check.sh` or a reviewed source script in the repository.

Confirm the effective identity and region before making changes:

```bash
aws sts get-caller-identity
aws configure list
aws s3api get-bucket-location \
  --bucket ashish-server-health-logs
```

An S3 bucket-location result of `null` means `us-east-1`; otherwise, verify the reported Region. The project configuration described here expects the bucket in `us-east-2`. Specify `--region us-east-2` for S3 and SNS operations against these resources, even if EC2 runs in another Region.

## 2. Configure the EC2 instance role

Attach an IAM role to the EC2 instance through an instance profile. Grant only the permissions the script needs. For report uploads, the role typically needs this permission scoped to the report prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UploadHealthReports",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::ashish-server-health-logs/logs/*"
    }
  ]
}
```

If the script performs other AWS operations, add only the specific required actions. Do not use `AdministratorAccess` as a shortcut. Test that the instance role is active:

```bash
aws sts get-caller-identity
aws s3 ls s3://ashish-server-health-logs/logs/ --region us-east-2
```

The `s3 ls` command is a separate permission check; it can fail if `s3:ListBucket` is intentionally not granted even when `s3:PutObject` works. Do not add list permission unless it is needed.

## 3. Install and test the monitoring script

Review the script before installing it. The current configuration described for this project uses:

- Script: `/usr/local/bin/server_health_check.sh`
- Report bucket: `ashish-server-health-logs`
- Report prefix: `logs/`
- AWS Region for S3: `us-east-2`
- Disk threshold: `80%`
- Memory threshold: `80%`
- Monitored service: `ssh`

Check the configuration and syntax:

```bash
sudo grep -nE 'AWS_REGION|S3_BUCKET|S3_PREFIX|THRESHOLD|SERVICE' \
  /usr/local/bin/server_health_check.sh
sudo bash -n /usr/local/bin/server_health_check.sh
```

Ensure the configuration guard checks for empty or placeholder values, not the correct configured values. For example, a validation condition should not treat `AWS_REGION="us-east-2"` or `S3_BUCKET="ashish-server-health-logs"` as an error simply because those strings match the intended configuration.

Run a manual test and inspect its exit status and logs:

```bash
sudo /usr/local/bin/server_health_check.sh
printf 'Script exit code: %s\n' "$?"
sudo tail -n 80 /var/log/server_health.log
```

If the script relies on a state directory, verify that the configured execution user can create and update it. In the described setup, the state file is under `/var/lib/server-health-check/alert-state` and the lock file is under `/var/lock/server_health_check.lock`.

## 4. Configure scheduled execution

The documented deployment uses `/etc/cron.d/server-health-check` to run the script every minute. A cron file uses a user field, unlike a user's personal crontab. Use `flock` to avoid overlapping runs:

```cron
* * * * * root /usr/bin/flock -n /var/lock/server_health_check.lock /usr/local/bin/server_health_check.sh >> /var/log/server_health_cron.log 2>&1
```

Create or update the file carefully:

```bash
sudoedit /etc/cron.d/server-health-check
sudo chmod 644 /etc/cron.d/server-health-check
sudo systemctl enable --now cron
sudo systemctl status cron --no-pager
```

Confirm the path to `flock` on the instance with `command -v flock`. Check cron output after a minute:

```bash
sudo tail -n 80 /var/log/server_health_cron.log
```

Configure log rotation for application and cron logs so continuous scheduling does not cause unbounded disk growth. Validate the existing `/etc/logrotate.d/server-health-check` configuration if present.

## 5. Create the SNS topics

Create two Standard SNS topics in `us-east-2`:

- `server-health-notifications` — receives the S3 event and invokes Lambda.
- `server-health-formatted-notifications` — receives Lambda's readable message and sends email.

The following CLI commands are useful if the topics do not already exist:

```bash
aws sns create-topic \
  --name server-health-notifications \
  --region us-east-2

aws sns create-topic \
  --name server-health-formatted-notifications \
  --region us-east-2

aws sns list-topics --region us-east-2 --output table
```

Use the ARNs returned by AWS. Do not assume a topic exists merely because its name appears in code. If AWS returns `NotFound`, verify the selected account and Region and create the missing topic if appropriate.

Subscribe the recipient to the formatted topic using protocol `email`:

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-formatted-notifications \
  --protocol email \
  --notification-endpoint YOUR_EMAIL_ADDRESS \
  --region us-east-2
```

Replace `YOUR_ACCOUNT_ID` and `YOUR_EMAIL_ADDRESS` with actual values in the command you run. Confirm the subscription using the message SNS emails to the recipient. The subscription should show protocol `email` and a confirmed subscription ARN. Do not use `email-json` for a human-readable text notification.

## 6. Configure the S3 event notification

In S3, open `ashish-server-health-logs` → **Properties** → **Event notifications** and create or review one notification with:

- Event type: `s3:ObjectCreated:*`
- Prefix filter: `logs/`
- Suffix filter: `.txt`
- Destination: SNS topic `server-health-notifications`

S3 and the SNS destination must be in the same Region. The bucket notification configuration and SNS topic policy must allow S3 to publish. The console can help apply the destination policy; if setting permissions manually, restrict the SNS policy to the intended bucket ARN and source account.

Do not point the S3 event notification at the formatted email topic. If report uploads trigger the formatted topic directly, Lambda will not get a chance to read the report and construct the message.

## 7. Create and configure the Lambda formatter

Create a Lambda function named `format-server-health-email` in `us-east-2`, using a supported Python runtime. Use this handler when the code file is `lambda_function.py`:

```text
lambda_function.lambda_handler
```

The function's execution role should have:

1. The AWS-managed policy `AWSLambdaBasicExecutionRole` for CloudWatch Logs.
2. `s3:GetObject` scoped to `arn:aws:s3:::ashish-server-health-logs/logs/*`.
3. `sns:Publish` scoped to the exact `server-health-formatted-notifications` topic ARN.

Example inline policy (replace the account ID if required):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadHealthReports",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::ashish-server-health-logs/logs/*"
    },
    {
      "Sid": "PublishFormattedEmail",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-formatted-notifications"
    }
  ]
}
```

In the Lambda console, add `server-health-notifications` as an SNS trigger. Deploy code that:

1. Reads the event envelope from `Records[].Sns.Message`.
2. Parses the embedded S3 event JSON.
3. Ignores the S3 `s3:TestEvent` payload.
4. Decodes the URL-encoded object key.
5. Retrieves the `.txt` object using `s3:GetObject`.
6. Builds a readable subject and report body.
7. Publishes to `server-health-formatted-notifications`.
8. Logs errors and successful publish operations to CloudWatch Logs.

The destination ARN inside the code must match the formatted topic's actual ARN. The Lambda should publish the report's contents, not simply forward the raw S3 event JSON.

## 8. Prevent raw JSON emails

Inspect subscriptions on both SNS topics:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-notifications \
  --region us-east-2 \
  --output table

aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-formatted-notifications \
  --region us-east-2 \
  --output table
```

Expected subscription arrangement:

- Raw topic: Lambda subscription only for this workflow.
- Formatted topic: confirmed `email` subscription.

Remove a direct `email` or `email-json` subscription from the raw topic if you do not want the raw SNS notification. Before removing anything, verify that the Lambda subscription is present and active.

## 9. End-to-end validation

First, test the formatted email topic independently. This isolates SNS email delivery from S3 and Lambda:

```bash
aws sns publish \
  --topic-arn arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-formatted-notifications \
  --subject '[TEST] Readable Server Health Alert' \
  --message $'SERVER HEALTH ALERT\n\nServer: ec2-server-test\n\nCPU ALERT: test message\n\nMEMORY ALERT: test message\n\nSERVICE ALERT: test message\n\nStatus: TEST ONLY' \
  --region us-east-2
```

If this arrives as raw JSON, fix the email subscription on the formatted topic before continuing. Protocol `email` should be selected—not `email-json`.

Next, upload a test report to the same prefix and suffix matched by the S3 notification:

```bash
cat > /tmp/health-format-test.txt <<'REPORT'
SERVER HEALTH TEST REPORT
Host: ec2-server-test
Timestamp: test run

[CPU ALERT]
Alert: test message only.
Status: TEST ALERT ONLY

[MEMORY ALERT]
Alert: test message only.
Status: TEST ALERT ONLY

[SERVICE ALERT]
Alert: test message only.
Status: TEST ALERT ONLY
REPORT

aws s3 cp /tmp/health-format-test.txt \
  "s3://ashish-server-health-logs/logs/format-test-$(date +%s).txt" \
  --region us-east-2
```

Verify each stage:

- The object appears in S3.
- Lambda has a new invocation.
- CloudWatch Logs show that the object was read and a formatted SNS publish succeeded.
- The formatted SNS topic reports a confirmed email subscription.
- The inbox receives a readable email whose body includes the test report.

Do not call the full pipeline validated based solely on a successful S3 upload or `sns:Publish` response. Check the final inbox delivery.

## 10. Cleanup and rollback

After testing, stop any temporary CPU or memory stress workload and remove disposable systemd test units created for the demonstration. For a test service, inspect it first:

```bash
sudo systemctl status demo-payment-failure-test.service --no-pager
```

Use the exact unit name created by your test script; do not copy the example name if your generated unit is different. Reset a failed unit's state after inspection if needed:

```bash
sudo systemctl reset-failed ACTUAL_TEST_UNIT.service
```

For AWS cleanup, first disable the S3 event notification if you no longer need the pipeline, then remove unneeded Lambda triggers, SNS subscriptions/topics, Lambda functions, and IAM policies/roles after confirming they are dedicated to this project. Do not delete a shared role or notification configuration used by other workloads. Review S3 objects and CloudWatch log retention to control storage costs.

## Deployment acceptance checklist

- [ ] EC2 role works without static keys.
- [ ] The manual health check runs successfully.
- [ ] cron and `flock` operate as expected.
- [ ] S3 objects use the intended bucket, prefix, and suffix.
- [ ] S3 sends object-created events to the raw SNS topic.
- [ ] Lambda is subscribed to the raw topic and can read the report.
- [ ] Lambda can publish to the formatted SNS topic.
- [ ] The formatted topic has a confirmed `email` subscription.
- [ ] A test upload produces a readable email.
- [ ] The repository contains no credentials or sensitive logs.
