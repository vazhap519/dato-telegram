# Tribute automatic subscription handling — stage 2 (safe rollout)

## Overview

The first paid, real-world $1 checkout on 2026-10-28 completed:
**our Telegram bot -> Tribute -> signed new_subscription webhook ->
owner-confirmed paid record -> bot-created one-use invite -> successful join**.
The customer cancelled future renewals; access remains valid until
2026-11-28. This payment has already been processed and is never replayed.

The new automation is a **durable background consumer** in the Telegram bot
process. The existing public POST /webhooks/tribute endpoint **only** validates
the HMAC and inserts a sanitized idempotent event into PostgreSQL, so a bot
crash cannot lose a received event. Every ~10s the bot checks for eligible
inbox events and separately retries failed Telegram invitation delivery.
No code in this stage bans/kicks/unbans group members.

Two independent safety switches, BOTH OFF by default:
1. `tribute_auto_offers.enabled` per exact Tribute subscription_id, period_id,
   channel_id, amount in minor units, currency and cadence.
2. `tribute_auto_settings.enabled` global switch.

**The pre-existing paid $1 event and all historic events are excluded** from
automatic backfill by both `enabled_at` thresholds; prior event status
`processed` is never reprocessed.

The $1 test offering is preloaded in a DISABLED state. It stays hidden from
the public tariff list and requires an owner-issued, short-lived private bot
test link. There is no reason to re-charge or re-enable it merely to test
deployment. Other existing Tribute products (USD/EUR "Вход в Круг Души",
"Подписка Первопроходца") have NO automatic mapping or privileges.

### Strict acceptance for production NEW purchases

- Signed `new_subscription` from Tribute, persisted with signature_valid=True.
- Event must match an explicitly enabled, mapped offer AND period AND channel.
- Exact type regular, amount integer minor units, correct currency and period.
- `telegram_user_id` identifies the real Telegram account. `trb_user_id`
  (not obsolete `user_id`) is the stable Tribute buyer identity.
- `expires_at` must describe a credible newly paid period; no expired,
  future-created, gift or trial grants.
- Buyer must already have started our Telegram bot, accepted the CURRENT offer,
  and clicked this plan in the bot in the prior 48 hours.
- Mapping revalidates current bot plan price/currency and an HTTPS Tribute URL.
- A database transaction locks the event, serializes the buyer, records or
  extends exactly one `subscriptions` entry with paid_until, records the
  receipt and creates a durable Telegram invite job.
- A retry/duplicate event cannot extend access twice or change buyer identity.
- Only successful, committed paid records qualify for invites.

### Strict acceptance for RENEWALS

- Signed `renewed_subscription` with same exact offer/period/channel/amount,
  trb_user_id and matching telegram_user_id.
- Existing `tribute_auto_buyers` binding MUST match; no new customer is
  inferred from an unknown renewal.
- The previously owner-approved $1 payment can also be recognized from the
  immutable `tribute_test_buyer_bindings` table if the same user ever renews.
- Extend `paid_until` with SQL GREATEST (never shorten a paid period); update
  the current tariff plan if the signed period changes.
- No extra invoice or customer checkout is initiated by our bot. Tribute
  controls recurring billing and sends the next event.

**Important: we have NOT received a genuine renewal webhook yet.** Contract
tests and offline simulation cover its behavior, but real-world confirmation
requires a real future renewal or later careful reconciliation.

### What cancellation does

Tribute support confirmed that `cancelled_subscription` comes at the END
of the purchased period, not when next renewals are switched off.
This stage intentionally never revokes membership on that event.
Every paid permission is time-bound via subscriptions.paid_until, and
existing lifetime access_grants are always evaluated independently.

The global membership removal control must REMAIN OFF pending further
reconciliation, expiry/grace tests, and first real renewal verification.
Someone may physically remain in the Telegram group after their paid
period ends while removal is disabled. This is intentional during rollout.

### Errors / Telegram outages

The paid DB transaction always commits BEFORE any Telegram invitation.
A failed send is saved in `tribute_invite_outbox` and retried every 30 minutes,
with claim leases for bot restarts. A buyer can request a fresh private invite
from `/subscription` after confirmed access is recorded.

Unexpected SQL/network failures leave the inbox event `pending_review`.
Invalid allowlisted events become `needs_attention` in the journal and never
grant access. Do not instruct buyers to pay again. Inspect the journal and
subscription_audit_log instead.

## Deployment (read-only first)

```bash
/usr/local/bin/update-dato-telegram
cd /opt/dato-telegram
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_tribute_auto.py
```

All tests and read-only catalogue comparisons must pass before any toggle.
The update creates new independent DB tables; it does not grant/revoke access
or overwrite .env. The update script keeps its own pre-update backup.

Admin panel: https://bot.arutyun.me/subscriptions/tribute-auto

- Confirm global switch is OFF, and first test offer is OFF.
- For a future real, public-channel-backed Tribute subscription, create or
  configure its matching active non-lifetime bot plan with exact cost/currency,
  period and Tribute payment link in main Subscriptions -> Tariffs.
- In Tribute, confirm the offer is linked to the intended **public channel**;
  the private "Круг Души" must remain under OUR bot's control.
- Obtain actual offer ID and period ID using read-only
  `scripts/check_tribute_subscriptions.py`; check the channel ID from an
  independently verified event or creator configuration.
- Use "Привязать новый действующий тариф Tribute" to enter the matching
  IDs, channel, exact price in cents, period, and existing bot plan.
- The new mapping is OFF by default. Validate it with
  `scripts/check_tribute_auto.py` and the Tribute checkout page.
- Enable this exact offer, then enable the global switch. Only NEW signed
  events arriving after BOTH opt-ins will be processed.

### Emergency stop

On the admin page, uncheck the global "Автоматическая выдача и продление" and
save it. This stops new automatic activations and invite sends; all existing
paid subscriptions and lifetime grants remain intact. Already-queued events
received while disabled are NOT automatically backfilled when re-enabled:
inspect the secure webhook journal for manual reconciliation.

Never use the group-removal settings to "test" these webhook toggles.
Never reuse the 1 USD test link as a real public product.

## Verification after deployment

Expect the following without any real charge:
- Test suite passes; live API catalogue matches the stored test offer.
- Global auto processing OFF; test offer OFF.
- Existing paid user's `/subscription` continues to show access until
  2026-11-28, and the member remains in "Круг Души".
- No new membership changes or payment confirmations appear in logs.
- The admin page opens with both toggles off.
