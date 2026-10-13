# Server Health Monitoring and Alerting Troubleshooting Runbook

## Triage workflow

When an alert email fails or arrives as raw JSON, do not change every component at once. Follow the signal through the system in order:

1. Did the health-check script run?
2. Did it upload the report to the expected S3 prefix?
3. Did S3 publish an event to the raw SNS topic?
4. Did that topic invoke Lambda?
5. Could Lambda read the report and publish to the formatted SNS topic?
6. Is the formatted topic's email subscription confirmed and using protocol `email`?
7. Did the readable message arrive in the inbox, including Spam/Junk?

This sequence isolates the failure without confusing a successful upload with successful email delivery.

## 1. Email still contains the SNS JSON envelope

### Symptoms

The message body contains fields such as `Type`, `MessageId`, `TopicArn`, `Message`, `Timestamp`, and `Signature` rather than the health report.

### Likely causes

- The email subscription is attached to `server-health-notifications`, the raw-event topic.
- The formatted topic uses protocol `email-json` rather than `email`.
- The Lambda code parses the event but forwards the raw SNS event JSON instead of reading the report from S3.
- The S3 event notification publishes directly to the formatted topic, bypassing Lambda.

### Diagnose

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

### Resolution

- Keep the Lambda subscription on `server-health-notifications`.
- Remove direct email subscriptions from the raw topic once the Lambda path is confirmed.
- On `server-health-formatted-notifications`, use protocol `email`, not `email-json`, and confirm the subscription from the inbox.
- In Lambda, parse `Records[].Sns.Message`, parse the nested S3 event, read the S3 object, and publish a human-readable message to the formatted topic.
- Verify that the S3 event notification's destination is the raw topic only.

A plain-text SNS email can have headings and line breaks. A branded HTML layout generally needs an email service designed for HTML content, such as Amazon SES.

## 2. `Topic does not exist` or `NotFound`

### Likely causes

- The topic has not been created.
- The CLI is targeting the wrong AWS account or Region.
- The ARN in the Lambda code contains a typo or incorrect account ID.

### Diagnose

```bash
aws sts get-caller-identity
aws sns list-topics --region us-east-2 --output table
```

### Resolution

Use the actual topic ARN returned by AWS. Create `server-health-formatted-notifications` in `us-east-2` if it is missing and you intend to use that name. Update the `OUTPUT_TOPIC_ARN` value in the Lambda Python code to match exactly, then deploy the code. Do not paste Python assignments such as `OUTPUT_TOPIC_ARN = (...)` into Bash; edit them in the Lambda code editor.

## 3. Direct test email works, but S3-to-Lambda email does not

A direct `aws sns publish` test to the formatted topic bypasses Lambda. If it is readable, the email delivery endpoint is probably correct; continue investigating the Lambda path.

### Check the Lambda invocation

In the AWS console, open **Lambda → `format-server-health-email` → Monitor → View CloudWatch logs**. Or run:

```bash
aws logs tail /aws/lambda/format-server-health-email \
  --since 30m \
  --region us-east-2
```

Interpret common results:

| Log or symptom | What to check |
|---|---|
| No invocation | Verify the raw SNS topic has the Lambda subscription and that the test file matches the S3 notification filter. |
| `AccessDenied` on `GetObject` | Check the Lambda execution role's `s3:GetObject` permission for `arn:aws:s3:::ashish-server-health-logs/logs/*`. |
| `AccessDenied` on `Publish` | Check the role's `sns:Publish` permission for the formatted topic's exact ARN. |
| `JSONDecodeError` | Inspect the incoming SNS message shape; handle the S3 test event separately from ordinary records. |
| `NoSuchKey` | Ensure the code decodes URL-encoded object keys and uses the bucket/key from the incoming event. |
| Logs show publish success, but no message arrives | Check the output topic's email subscription status, protocol, spam folder, and topic ARN. |

The Lambda execution role usually needs `AWSLambdaBasicExecutionRole`, `s3:GetObject` for report objects, and `sns:Publish` on the formatted topic. Do not grant broad administrator permissions to work around a specific access error.

## 4. Formatted SNS topic has no subscription or is pending

Inspect the subscriptions in the SNS console or run:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-2:YOUR_ACCOUNT_ID:server-health-formatted-notifications \
  --region us-east-2 \
  --output table
```

The required endpoint should use protocol `email` and have a confirmed subscription ARN. If it is pending confirmation, open the confirmation email and confirm it. If needed, remove the unconfirmed subscription and subscribe again using `--protocol email`.

## 5. S3 report exists, but Lambda is not triggered

### Check the S3 notification

S3 → bucket `ashish-server-health-logs` → **Properties → Event notifications**. Confirm:

- Event type includes `s3:ObjectCreated:*`.
- Prefix is exactly `logs/`.
- Suffix is exactly `.txt`.
- Destination is `server-health-notifications` in `us-east-2`.

A file outside `logs/` or without a `.txt` suffix will not match these filters.

### Check permissions and Region

The SNS raw topic policy must allow `s3.amazonaws.com` to publish, scoped to the intended bucket and source account. S3 and its SNS destination must be in the same Region. An EC2 instance in `us-east-1` can upload to an S3 bucket in `us-east-2`, but the bucket operations must target `us-east-2`.

Avoid creating overlapping S3 notification filters to the same destination without checking existing notification rules.

## 6. `aws s3 cp` fails from EC2

### Diagnose

```bash
aws sts get-caller-identity
aws configure list
aws s3api get-bucket-location --bucket ashish-server-health-logs
```

### Common fixes

- `Unable to locate credentials`: attach or repair the EC2 instance profile; do not paste long-lived keys into the repository.
- `AccessDenied`: check that the instance role allows `s3:PutObject` on `arn:aws:s3:::ashish-server-health-logs/logs/*` and that bucket policies or organization policies do not deny the request.
- Wrong Region or redirect errors: use `--region us-east-2` for this bucket.
- Wrong bucket name: verify the exact bucket name and AWS account.

A successful upload only confirms that S3 accepted the object. It does not confirm SNS, Lambda, or email delivery.

## 7. cron does not run the script

Check syntax, permissions, cron service status, and logs:

```bash
sudo bash -n /usr/local/bin/server_health_check.sh
sudo systemctl status cron --no-pager
sudo cat /etc/cron.d/server-health-check
sudo tail -n 100 /var/log/server_health_cron.log
sudo tail -n 100 /var/log/server_health.log
command -v flock
```

In `/etc/cron.d/server-health-check`, ensure the schedule includes the user field, for example `root`, and that all command paths are absolute. The cron file should have appropriate permissions, commonly `0644`. Check the configured log and state directories for ownership and writable permissions.

Use `flock` to avoid concurrent executions if a health check can take longer than the schedule interval. If the lock is held, inspect whether a previous run is still active before changing or deleting lock files.

## 8. Repeated notifications or no repeated notifications

The project uses alert-state/cooldown handling where configured. Repeated alerts may occur if the cooldown file is missing, reset, unwritable, or stored in a different location between runs. No new alert may occur while a cooldown window is active.

Check the script's configured state path, for example `/var/lib/server-health-check/alert-state`, and review the code before manually deleting it. Resetting alert state can cause a new notification on the next run. Document any reset as part of the test so results are reproducible.

## 9. CPU alert does not occur

The described production health-check script checks disk usage, memory usage, and the SSH service. The temporary CPU stress test creates load, but it does not make the regular script alert on CPU unless CPU measurement and threshold logic have been implemented in the script.

To support real CPU alerts, add an explicit CPU metric measurement, define the evaluation window and threshold, apply cooldown handling, include the measured result in the report, and test both below-threshold and above-threshold cases. Until then, label CPU lines in demonstration reports as simulated or test-only.

## 10. Memory or service alert is labeled simulated

That label is intentional when the test report is generated with fixed text. A simulated report demonstrates transport and formatting, not detection accuracy.

To validate detection:

- For memory, record actual total/available memory and compare the computed usage to the configured threshold.
- For services, query the exact service managed by systemd and record its observed state. Use a disposable test unit rather than stopping SSH or an application service.
- Verify that the normal monitoring script—not a separate report generator—detects the condition and creates the report.

Do not attempt to exhaust EC2 memory or disable SSH simply to generate a screenshot. A bounded test load and a disposable service are safer for a portfolio demonstration.

## 11. Email subject or body is truncated or difficult to read

SNS subjects have length restrictions. Keep the subject concise, and put details in the message body. Use simple headings and line breaks for a text email. Confirm that the formatter publishes the actual report content rather than the serialized event. For HTML rendering, use an HTML-capable delivery service such as Amazon SES and escape report text before inserting it into HTML.

## 12. Evidence to collect after a fix

Capture authentic screenshots or sanitized logs showing:

- The health report object in the S3 bucket.
- The original SNS topic's Lambda subscription.
- The formatted SNS topic's confirmed `email` subscription.
- A successful Lambda invocation in CloudWatch Logs.
- A readable email with CPU, memory, and service sections.

Remove account-specific information, email addresses, public IPs, tokens, and other sensitive details before committing evidence to a public repository.

## Incident record template

Use this template in your project notes for each issue:

```text
Incident:
Observed symptom:
Expected behavior:
Affected component:
Evidence / log excerpt (sanitized):
Root cause:
Fix applied:
Validation steps:
Follow-up action:
```
