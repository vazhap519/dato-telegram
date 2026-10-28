# Tribute webhook integration — stage 1 (observation only)

## Scope

The FastAPI service at `bot.arutyun.me` now exposes:
- `POST /webhooks/tribute` — public HMAC-verified webhook receiver.
- `GET /subscriptions/tribute-events` — authenticated read-only audit screen.

**Stage 1 does not grant, extend, cancel or remove Telegram access.**
All subscription events are stored with `pending_review`. Non-subscription events are marked `ignored`.
The existing automatic removal setting remains unchanged. Do not enable it as part of this rollout.

## Server configuration

Set `TRIBUTE_API_KEY` in the bot server's private environment file (never GitHub or chat).
The value is the API key shown in Tribute Creator Dashboard → Settings → API Keys.
Do not paste the key into support messages or logs.

No `TRIBUTE_API_KEY`: incoming calls return 503; the admin screen remains viewable.
Never configure Tribute to send webhooks before the deployment and HTTPS ingress are verified.
Check that a pre-existing Tribute webhook URL is not being used for another integration before replacing it.

Webhook URL: `https://bot.arutyun.me/webhooks/tribute`

The webhook requires `Content-Type: application/json`, `trbt-signature` with HMAC-SHA256 of the **raw bytes** signed by the API key, and a JSON envelope containing `name`, `created_at` and `payload`.
The handler accepts hex or Base64 encoding of the digest; invalid signatures return 401.
Body size is limited to 64 KiB. Valid signed payloads are stored transactionally with a stable event hash excluding `sent_at`, making retries idempotent.

Only selected identifiers, key names and timestamps are saved; names, email addresses and the full original payload are not retained.
The existing PostgreSQL table `payment_webhook_events` is used (created on application startup by `initialize_subscriptions`).

## Verification

Local unit tests:
```bash
python -m unittest discover -s tests -p 'test_tribute_webhooks.py' -v
```

For real integration tests, use a dedicated Tribute subscription and test Telegram group.
Verify 401 for an invalid signature and a single `pending_review` entry for a valid delivery and retry.
Check `expires_at`, `telegram_user_id`, `trb_user_id`, `subscription_id` names as actually delivered.
**Never treat `trb_user_id` as a Telegram ID**; these are distinct identifiers.

## Next steps (not implemented)

- Validate real payload fields and the dedicated public-channel subscription ID.
- Correlate with the bot's Telegram IDs; do not grant access for an unknown identity.
- Build transactional entitlement processor that rejects stale/out-of-order events.
- Verify expiration, cancellation timing, grace period, lifetime entitlements, gifts/trials, event replay and failure recovery.
- Test in a dedicated group before migration of existing members and subscriptions.

## Emergency stop

Remove the webhook URL from Tribute's creator configuration to stop new deliveries, or unset `TRIBUTE_API_KEY` and restart the FastAPI service (receiver returns 503).
Note that Tribute may retry delivery for approximately 24 hours.
Existing subscription/access records are **never** changed by this module.
