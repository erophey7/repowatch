# Webhook events and delivery

Set `notify_webhook_url` in YAML to enable JSON POST notifications. Treat the
URL as a secret. It is not returned by the settings API or edited in the
dashboard. No new service or Python dependency is required.

```yaml
notify_webhook_url: https://receiver.example/hooks/your-token
notify_after_failures: 3
notify_events:
  - repository.failing
  - repository.recovered
  - repository.changed
  - warm.started
  - warm.completed
```

`notify_events` replaces the whole selection. Its default is
`[repository.failing, repository.recovered]`, preserving existing notification
volume. An empty list disables delivery while retaining failure bookkeeping.
Unknown names, duplicate names and non-list values are rejected. These settings
are YAML-only. Configuration is read by subsequent operations; an in-progress
operation keeps its notification configuration.

## Event catalog

| Event | Trigger and fields |
| --- | --- |
| `repository.failing` | An unnotified failure streak reaches `notify_after_failures`. Existing `kind`, `status: failing`, `consecutive_failures`, `message` and `text` fields are retained. Kinds are `prefetch`, `gpg` (also Nix signatures) and `key_expiry`. This is not an alert for every possible upstream failure. |
| `repository.recovered` | Success resets a streak whose failing notification was delivered. Retains `kind` and `status: recovered`. Enabling only recovery events does not create a failing notification or mark a streak notified. |
| `repository.changed` | A changed snapshot has been recorded, before replacement processing and ordinary warming. Includes counts `added`, `removed`, `modified` and current `packages`. Initial population counts as a change; unchanged checks emit nothing. A notification is not proof that warming has completed. |
| `warm.started` | A nonempty eligible warm operation is about to run. Includes `operation_id`, `requested`, `eligible` and `manual` (`force=True`). Applies to automatic, manual and replacement warming, including Nix. Disabled automatic warming and completely excluded selections emit nothing. |
| `warm.completed` | The operation returned, including partial or complete download failures. Includes the same operation fields, `succeeded`, `failed`, `skipped`, and `status`: `completed`, `partial` or `failed`. An unexpected exception emits `status: error` without result counts, then propagates to the original caller. |

For Nix, counts refer to selected catalog roots/outputs, not individual NARs
or shared dependencies. `skipped` counts requested keys absent from the final
outcome map. A process stop or task cancellation can leave a start event without
a completion event. Separate concurrent operations have separate IDs; their
events may interleave. Filters can change independently between operations.

## Envelope and receiver deduplication

Every payload adds `schema_version: 1`, `event`, a UUID `event_id`, UTC
`occurred_at`, and `repo_id`. Every payload has human-readable `text`, retaining
Slack/Mattermost compatibility. Discord requires its Slack-compatible webhook
endpoint (`/slack`), not its ordinary endpoint. Lifecycle pairs share an
`operation_id` but each event has its own `event_id`.

All delivery attempts for one event use identical JSON and an
`Idempotency-Key` header equal to `event_id`. The receiver should store processed
IDs and ignore repeats: the sender cannot know whether a timed-out POST was
accepted. The header alone does not make Slack or another receiver deduplicate.
This is not an exactly-once delivery guarantee.

## Retry policy and limits

Delivery makes at most three attempts, each with a 10-second overall deadline.
Network/transport errors, HTTP 429 and HTTP 5xx are retried, normally after
one and two seconds. `Retry-After` seconds and HTTP dates are honored when
they require a longer wait, up to 30 seconds. A longer requested wait ends
this delivery rather than retrying earlier than requested. Invalid values fall
back to the normal delay. HTTP 2xx succeeds; other responses are terminal.
Redirects are not followed, preventing forwarding the payload to another URL.
Cancellation propagates normally. Delivery errors do not fail the repository
check or warm operation. Application delivery warnings omit the URL, response
body and exception text, since these may contain credentials.

Retries are awaited inline and can delay the originating operation (at most
about 90 seconds per event, excluding scheduling overhead). There is no durable
outbox, background delivery worker or replay after restart. After exhaustion,
change/lifecycle/recovery events are dropped with a warning. An undelivered
failing alert leaves its streak unnotified, so a later failure check may create
a new event with a new ID. A successful failing alert marks the streak notified;
recovery resets that state even if recovery delivery subsequently fails.
A receiver needing replay or stronger guarantees must persist incoming events;
this sender does not guarantee eventual delivery through a prolonged outage.
