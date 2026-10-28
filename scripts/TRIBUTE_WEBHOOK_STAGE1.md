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

## Identified test subscription (2026-10-28)

Read-only GET /subscriptions succeeded and returned exactly one USD 1/month offering:
- Test public-channel subscription: `subscriptionId=279302`
- Monthly pricing period: `periodId=528940`
- Payment link inside Telegram: `https://t.me/tribute/app?startapp=s1aES`
- Web payment link: `https://web.tribute.tg/s/1aES`

Existing creator offers **must not be mapped to this test community-access flow**:
- `subscriptionId=229558` (existing USD "Вход в Круг Души")
- `subscriptionId=183308` (existing EUR "Вход в Круг Души")
- `subscriptionId=236047` ("Подписка Первопроходца")

The catalogue identifies offerings, **not buyers**, so it cannot prove
who purchased nor activate an entitlement.

The public-channel subscription is recurring, and the checkout displayed
a total of approximately USD 1.12 at the time of the user's screenshot.
A real payment test is a real charge; perform only with the owner's approval,
preferably using an otherwise unentitled test Telegram account, and cancel
future renewals after the test.

The first real signed event must be inspected for its event name, `subscription_id`
and/or `subscriptionId`, `period_id` and/or `periodId`, `expires_at`,
and buyer identity before any payment-to-Telegram access automation is implemented.
Webhooks for unrelated existing offers remain in the audit inbox only.

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
