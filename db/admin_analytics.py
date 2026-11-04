"""Evidence-only Telegram admin analytics. No currency conversions or invented attribution."""
from sqlalchemy import text
from db.database import SessionLocal

async def report():
    async with SessionLocal() as s:
        totals=(await s.execute(text("""
          SELECT
            (SELECT COUNT(*) FROM users) total_users,
            (SELECT COUNT(DISTINCT telegram_id) FROM verified_payment_receipts WHERE payment_status='paid') buyers,
            (SELECT COUNT(*) FROM verified_payment_receipts WHERE payment_status='paid') paid_events,
            (SELECT COUNT(DISTINCT telegram_id) FROM
               (SELECT telegram_id FROM verified_payment_receipts WHERE payment_status='paid'
                GROUP BY telegram_id HAVING COUNT(*)>=2) b) repeat_buyers,
            (SELECT COUNT(*) FROM verified_payment_receipts WHERE payment_status IN ('refunded','chargeback')) reversals,
            (SELECT COUNT(*) FROM users WHERE created_at>NOW()-INTERVAL '30 days') registered_30d
        """))).mappings().one()
        by_currency=(await s.execute(text("""
          SELECT currency,provider,date_trunc('month',verified_at AT TIME ZONE 'Asia/Tbilisi') period,
            COUNT(*) FILTER(WHERE payment_status='paid') payments,
            COALESCE(SUM(amount) FILTER (WHERE payment_status='paid'),0) gross_amount,
            COUNT(*) FILTER (WHERE payment_status IN ('refunded','chargeback')) reversals
          FROM verified_payment_receipts
          WHERE verified_at >= NOW()-INTERVAL '12 months'
          GROUP BY currency,provider,date_trunc('month',verified_at AT TIME ZONE 'Asia/Tbilisi')
          ORDER BY period DESC,provider,currency LIMIT 120
        """))).mappings().all()
        funnels=(await s.execute(text("""
          SELECT
          (SELECT COUNT(*) FROM users) started,
          (SELECT COUNT(DISTINCT telegram_id) FROM offer_acceptances) accepted,
          (SELECT COUNT(DISTINCT telegram_id) FROM subscription_clicks) clicked,
          (SELECT COUNT(DISTINCT telegram_id) FROM verified_payment_receipts
            WHERE payment_status='paid') buyers
        """))).mappings().one()
        retention=(await s.execute(text("""
          WITH per_buyer AS (
            SELECT telegram_id,MIN(verified_at) first_paid,
                   MAX(verified_at) last_paid,COUNT(*) payment_count
            FROM verified_payment_receipts WHERE payment_status='paid'
            GROUP BY telegram_id
          )
          SELECT COUNT(*) FILTER(WHERE first_paid<=NOW()-INTERVAL '30 days') eligible_30d,
            COUNT(*) FILTER(WHERE first_paid<=NOW()-INTERVAL '30 days'
              AND last_paid>=first_paid+INTERVAL '30 days') repeat_30d,
            COUNT(*) FILTER(WHERE first_paid<=NOW()-INTERVAL '90 days') eligible_90d,
            COUNT(*) FILTER(WHERE first_paid<=NOW()-INTERVAL '90 days'
              AND last_paid>=first_paid+INTERVAL '90 days') repeat_90d
          FROM per_buyer
        """))).mappings().one()
        return {"totals":dict(totals),"by_currency":[dict(x) for x in by_currency],
                "funnel":dict(funnels),"retention":dict(retention)}

async def export_subscribers():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT u.telegram_id,u.username,u.first_name,u.last_name,u.created_at,
                 COALESCE(p.tags,'') tags,
                 EXISTS(SELECT 1 FROM verified_payment_receipts v
                   WHERE v.telegram_id=u.telegram_id AND v.payment_status='paid') verified_buyer,
                 EXISTS(SELECT 1 FROM access_grants g
                   WHERE g.telegram_id=u.telegram_id AND g.resource='community'
                     AND g.revoked_at IS NULL AND g.starts_at<=NOW()
                     AND (g.ends_at IS NULL OR g.ends_at>NOW()))
                   OR EXISTS(SELECT 1 FROM subscriptions sub
                   WHERE sub.telegram_id=u.telegram_id AND sub.status='active'
                     AND sub.paid_until>NOW()) access_now
          FROM users u LEFT JOIN subscriber_admin_profiles p ON p.telegram_id=u.telegram_id
          ORDER BY u.telegram_id DESC
        """))).mappings().all()
        return [dict(x) for x in rows]

async def export_verified_payments():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT telegram_id,provider,provider_payment_id,amount,currency,
                 payment_status,verified_at FROM verified_payment_receipts
          ORDER BY id DESC
        """))).mappings().all()
        return [dict(x) for x in rows]
