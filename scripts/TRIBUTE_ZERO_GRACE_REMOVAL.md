# Paid Tribute expiration: zero grace, kick on exact paid_until

## Product rule

For **new** "Круг Души" Tribute production subscription `279348`:

- Honor the exact `paid_until` timestamp received in the verified signed
  Tribute webhook, regardless of monthly/quarterly/half-yearly/yearly label.
- If the webhook confirms renewal before expiration: the paid_until increases
  and the member remains in the closed Telegram group.
- If paid_until has passed and NO new verified renewal is recorded, remove
  the member on the bot worker's next scan (every 30 seconds). **No additional
  seven-day / 24-hour / 72-hour grace period.**
- If Tribute later succeeds with a retry and sends a signed renewal after
  the removal, the bot records the renewed paid period and sends a fresh
  single-use invite; it does not silently add a member back to Telegram.
- The bot cannot know that a remote payment succeeded until a webhook is
  delivered. Delayed or missing webhooks can therefore lead to removal. We
  suppress removal if an already received, correctly signed new/renewed
  event for the same buyer is still pending review; this is NOT a grace
  period, but avoiding deletion while a payment is actively being processed.
- Clicking the payment link or a pending card charge does NOT authorize
  access. A cancellation does not extend paid_until.

### Isolation and protection

The removal selection requires ALL of:
1. A *deliberately enabled* dedicated `tribute_paid_removal_settings` switch.
2. Main Tribute automatic processing is enabled and legacy general removals OFF.
3. The configured closed group chat matches `subscription_managed_members`.
4. The bot itself observed a join via its recorded one-use invitation.
5. An immutable buyer binding in `tribute_auto_buyers` for **offer 279348**.
6. That exact `subscriptions` row is active, comes from Tribute and has a
   finite `paid_until <= NOW()`.
7. No current or future, nonrevoked `access_grants` rights for the community
   (including lifetime/first-pioneer grants), and no other active paid or
   perpetual subscription.
8. No signed pending/needs_attention new payment or renewal event for that
   user and this exact offer (requires manual reconciliation).
9. A SECOND entitlement and expiry check immediately before a Telegram ban.
10. Telegram confirms the person is neither administrator nor owner.

The worker calls Telegram `ban_chat_member` followed by
`unban_chat_member(only_if_banned=True)` to *remove but not permanently
block* the user. They may rejoin only after new verified payment and
receiving a fresh invite. Telegram Bot API rights to ban members are
verified before the owner can enable this feature.

**No automatic removal of people who joined under legacy Tribute control,
existing permanent members, users with any other paid access, or users
outside the new 279348 production offer.** The cancelled old $1 test
membership 279302 is explicitly NOT included in the new removal feature.

### Safe rollout

After GitHub commit deploy:

```bash
/usr/local/bin/update-dato-telegram
cd /opt/dato-telegram
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_tribute_paid_removal.py
```

When first deployed, the new removal toggle is OFF, by design. No one is
kicked by the update. Before enabling, check that:
- the new paid subscription processing is ON;
- the old global group-removal setting is OFF;
- @krugdushi_bot is an admin with **ban members** permission in the closed group;
- the worker has observed bot-issued invite joins
  (see "Tracked paid, bot-invited members" in the read-only script);
- existing lifetime grants are recorded for permanent members.

In `https://bot.arutyun.me/subscriptions/tribute-auto`, only after the
above checks, enable **Исключать при окончании оплаченного срока (0 часов
отсрочки)**. This is a separate switch from legacy general removals.

Turning this switch OFF stops further paid-member removals; existing paid
subscriptions remain intact. If the main Tribute auto-processing is disabled
for emergency maintenance, paid-only removals also stop automatically.

**Destructive-operation caution:** Enabling the new toggle can immediately
remove *already expired*, bot-managed 279348 buyers. No grace period is
applied. Test with an intentionally expiring controlled account before
relying on this with paying customers.
