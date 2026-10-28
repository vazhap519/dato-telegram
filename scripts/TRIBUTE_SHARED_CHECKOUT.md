# One Tribute payment link for four Krug Dushi durations (2026-10-28)

### Verified creator catalogue

Public-channel Tribute subscription (NOT the legacy closed-group subscriptions):
- Offer `subscriptionId=279348`, `Круг Души — закрытое сообщество`
- Telegram payment link: `https://t.me/tribute/app?startapp=s1aFC`
- Browser fallback: `https://web.tribute.tg/s/1aFC`
- One month: `periodId=529035`, 19 EUR
- Three months: `periodId=529034`, 49 EUR
- Six months: `periodId=529033`, 95 EUR
- One year: `periodId=529032`, 179 EUR

The screenshot of the checkout shows the same **public**
`Познай СЕБЯ • ПРАКТИКУМ` channel as the live $1 test;
the signed $1 webhook recorded `channel_id=547415`.
The setup script validates that the signed test event remains in our DB.
This establishes a strong mapping to the public channel, but the channel ID
for offer 279348 has NOT yet been observed in a real signed payment; inspect
the first actual payment carefully and stop processing if it differs.

Historic offers `183308`, `229558`, `236047` belonged to previous
private-group setups. NEVER map them to this new automatic checkout.

### Desired member journey

1. User starts @krugdushi_bot and accepts the current published offer.
2. Taps **Оформить подписку**.
3. If the owner enabled all safe switches, bot logs a fresh
   `tribute_checkout_intents(telegram_id,subscription_id=279348,offer_version,...)`
   and displays ONE URL button.
4. User selects any of the four durations *inside* Tribute.
5. Only signed `new_subscription` events for offer 279348 and one of the
   exact period/price pairs qualify for a grant. A new buyer must have
   accepted the current terms and created an offer-level intent in the bot
   within 48 hours before the real payment's delivery.
6. A DB transaction persists a single subscription/buyer binding,
   the paid_until date **from the signed event**, a receipt, and an invite
   outbox entry. Invitations are sent asynchronously and retried if needed.
7. A verified `renewed_subscription` for the same buyer may extend
   paid_until and change from monthly to annual, without another user click.
8. Switching renewal OFF in Tribute does NOT immediately remove access.
   Paid_until remains the effective term. Removal from the Telegram group
   is **OFF**; lifetime members are not touched.

Stale inline buttons for selecting bot plans can no longer charge the old
closed-group offers; they show a message to reopen /start.

### Deploy and test

```bash
/usr/local/bin/update-dato-telegram
cd /opt/dato-telegram
.venv/bin/python -m unittest discover -s tests -v

# Fresh, read-only verification of real Tribute data and public channel
.venv/bin/python scripts/setup_tribute_shared_checkout.py

# Only after dry-run PASS, explicit provision with no automated payments
.venv/bin/python scripts/setup_tribute_shared_checkout.py --apply

# Check four OFF-by-default mappings and accurate API prices
.venv/bin/python scripts/check_tribute_auto.py
```

Both services create the checkout tables at startup. The preparation
creates or reuses four specifically named bot plan rows (prices/durations
checked, never overwrites an existing plan) and corresponding Tribute
allowlist rows. It refuses to run if there is a conflicting plan, linked
private-group offer, old/unverified API catalogue, active global switch, or
an already enabled mapping. It does NOT enable any switches.

### Owner-controlled activation order

Protected admin panel:
https://bot.arutyun.me/subscriptions/tribute-auto

1. Check that **all four new periods** are listed correctly:
   IDs `529035`, `529034`, `529033`, `529032`; 19/49/95/179 EUR.
2. Enable each of the four *specific* production offer rows. Leave the $1
   test plan and legacy offers disabled.
3. Enable the main **Автоматическая выдача и продление** switch.
4. Enable the separate **Одна ссылка оплаты** switch. Only when all
   four mappings/plan prices and URL still match exactly will the bot
   permit payment entry.
5. From a second TEST account, open the bot and accept the terms. Tap
   **Оформить подписку** and **verify the four Tribute prices and title
   visually before authorizing any real payment**. Do not pay unnecessarily.
6. For the first real buyer, monitor the signed webhook journal, the
   subscriptions ledger, and the invite outbox. Never assume a purchase
   succeeded solely from a checkout click.
7. If a mismatch occurs, disable the shared checkout in admin; then disable
   the global switch. Paid access already recorded remains intact.

**Caveat:** Old saved bot `subscription_plan_selections` no longer counts
as proof of a new shared-offer checkout. The actual buyer must have opened
the shared offer from the bot, even if the customer later chooses another
period on the Tribute checkout. This blocks unrelated direct checkout
purchases from automatically granting group membership without consent.

No new real charge is needed for provisioning. There is still no live
renewal webhook to validate 2026 yearly/quarterly renewal behaviors:
the four-period and cross-period renewal contract tests are simulations,
not proof of live Tribute delivery.

### Known limitations before general availability

- The same shared checkout link is bound to all four durations. The bot
  must not promise a specific period until the signed webhook arrives.
- If an already paid new customer never opened the bot before paying, or
  if the offer ID/channel/amount differs, the webhook goes to manual
  review rather than granting access. Do not ask them to pay a second time.
- A missed or delayed webhook might require a manual admin reconciliation.
  The system does not query Tribute for arbitrary buyer subscriptions.
- Existing lifetime access_grants take precedence; no removals are enabled.
