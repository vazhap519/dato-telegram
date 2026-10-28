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

## Isolated checkout through our Telegram bot

The administrator can create an **unlisted** 20-minute, one-use bot entry via
`https://bot.arutyun.me/subscriptions/tribute-test` (requires admin login,
CSRF token and existing Nginx Basic Auth). The page shows the generated Telegram
deep link **only at creation time**, and the database stores only its SHA-256 hash.

The Telegram user should have accepted the current offer through `/start`.
If not yet accepted, the test link stays unclaimed: accept the offer first,
then reopen the same link while it is still valid. The link is atomically
claimed for the Telegram ID and cannot be reused.

After the claim, the bot displays the two previously verified Tribute URLs.
The checkout is a **real USD 1 monthly recurring payment**, and actual checkout
fees may increase the amount. The customer must actively authorize payment
in Tribute. The test must **never be inserted in the public subscription
plan list**, and claiming a link never grants Telegram group membership.

The private admin page lists who claimed a test link (Telegram ID) and when.
The webhook inbox separately lists signed payment/donation events: manually
compare only after the first real verified delivery. Tribute does not accept
a custom order ID in the payment link, so a claimed bot link and a checkout
page opening are not proof that a particular person paid.

A buyer should turn off future recurring charges after the controlled test
if not intentionally keeping the subscription.

## First REAL $1 payment received (2026-10-28)

A genuine signed `new_subscription` event was received in the secure journal
at approximately 17:44:35 UTC, for the private test offering:
- `subscription_id=279302`, `period_id=528940`, `channel_id=547415`
- `type=regular`, `period=monthly`, `currency=usd`, `amount=100` (minor units)
- `telegram_user_id` was supplied as a distinct field from `user_id` and
  `trb_user_id`; use **only** `telegram_user_id` for our Telegram bot user.
- The event's `expires_at` specified a period through November 28, 2026
  with nanosecond precision; Python timestamp parsing truncates to microseconds.

The event was inserted as `pending_review`. It is already paid, so a second
charge is **unnecessary**.

### Owner-reviewed initial activation (not global automation)

This guarded step can be run **only after** all local tests pass:

1. Admin visits `/subscriptions/tribute-events`.
2. For the exact test-offer event, clicks
   **Учесть платёж и отправить приглашение**.
3. The backend revalidates the signed saved record, expected test offer, period,
   channel, event type, exact amount, currency, expiry interval, buyer IDs,
   existence in our bot users table, and a bot-test-link claim by the same
   Telegram ID immediately before the purchase.
4. It atomically inserts/extends **only** a paid `subscriptions` record for
   this one buyer, a hidden inactive `subscription_plans` record, a payment
   receipt and audit record, and marks the inbox event `processed`.
5. Once committed, it sends an ordinary one-use direct group invitation.
   If sending fails, the already verified paid access is not undone: the owner
   may retry using the same journal button; the user may obtain an invite
   from their `/subscription` panel.

No public tariffs are modified. No existing `access_grants`, lifetime rights,
other offers, or existing group memberships are modified or revoked.
**No automatic cancellation or removal is enabled.** Future subscription events
continue to be recorded for review until a separate, well-tested automatic
renewal/expiry processor is deployed.

If owner approval returns HTTP 400, **do not pay again**. Diagnose the
mismatch between the stored signed event and the test link claim first.

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
