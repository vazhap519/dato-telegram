"""Templates and opt-in event-triggered reminders, independent of practicum schedules."""
from sqlalchemy import text
from db.database import engine, SessionLocal

TRIGGERS = {
    "no_offer_24h": "24 часа после /start, оферта не принята",
    "no_click_24h": "24 часа после принятия оферты, подписка не открыта",
}
DEFAULT_TEMPLATES = [
    ("Напоминание об оферте", "Здравствуйте, {first_name}! Если хотите продолжить оформление участия, ознакомьтесь с условиями в боте: /start."),
    ("Как оформить подписку", "Здравствуйте, {first_name}! Вы приняли условия. Для перехода к оформлению подписки воспользуйтесь кнопкой в сообщении бота."),
    ("Новая книга или медитация", "Здравствуйте, {first_name}! У меня появились новые материалы. Подробнее: {link}"),
    ("Приглашение на Zoom-встречу", "Здравствуйте, {first_name}! Приглашаю вас на встречу. Подробности: {link}"),
    ("Новость сообщества", "Здравствуйте, {first_name}! Хочу поделиться с вами новостью."),
]

async def initialize_automation():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_templates (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reminder_rules (
                trigger TEXT PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                enabled_at TIMESTAMPTZ,
                body TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reminder_deliveries (
                trigger TEXT NOT NULL,
                telegram_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (trigger, telegram_id)
            )
        """))
        await conn.execute(text("ALTER TABLE reminder_rules ADD COLUMN IF NOT EXISTS enabled_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS campaign_kind TEXT NOT NULL DEFAULT 'manual'"))
        await conn.execute(text("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS recipient_id BIGINT"))
        await conn.execute(text("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS button_text TEXT"))
        await conn.execute(text("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS button_url TEXT"))
        for name, body in DEFAULT_TEMPLATES:
            await conn.execute(text("""
                INSERT INTO message_templates (title, body)
                SELECT :name, :body WHERE NOT EXISTS
                    (SELECT 1 FROM message_templates WHERE title=:name)
            """), {"name": name, "body": body})
        for trigger, body in (
            ("no_offer_24h", "Здравствуйте, {first_name}! Напоминаю, что вы можете ознакомиться с публичной офертой, отправив /start."),
            ("no_click_24h", "Здравствуйте, {first_name}! Если хотите продолжить оформление участия, кнопка подписки доступна в сообщении бота."),
        ):
            await conn.execute(text("""
                INSERT INTO reminder_rules (trigger, enabled, body)
                VALUES (:trigger, FALSE, :body)
                ON CONFLICT (trigger) DO NOTHING
            """), {"trigger": trigger, "body": body})

async def templates():
    async with SessionLocal() as s:
        return [dict(row) for row in (await s.execute(text(
            "SELECT id, title, body FROM message_templates ORDER BY id"
        ))).mappings()]

async def upsert_template(template_id, title, body):
    if not (1 <= len(title.strip()) <= 120 and 1 <= len(body.strip()) <= 4000):
        raise ValueError("Неверная длина названия или текста")
    async with SessionLocal() as s:
        if template_id:
            result = await s.execute(text("""
                UPDATE message_templates SET title=:title, body=:body WHERE id=:id
                RETURNING id
            """), {"id": template_id, "title": title.strip(), "body": body.strip()})
            if result.scalar_one_or_none() is None:
                raise ValueError("Шаблон не найден")
        else:
            await s.execute(text(
                "INSERT INTO message_templates(title,body) VALUES (:title,:body)"
            ), {"title": title.strip(), "body": body.strip()})
        await s.commit()

async def rules():
    async with SessionLocal() as s:
        return [dict(row) for row in (await s.execute(text(
            "SELECT trigger, enabled, body FROM reminder_rules ORDER BY trigger"
        ))).mappings()]

async def set_rule(trigger, enabled, body):
    if trigger not in TRIGGERS or not (1 <= len(body.strip()) <= 4000):
        raise ValueError("Неверные параметры сценария")
    async with SessionLocal() as s:
        await s.execute(text("""
            UPDATE reminder_rules SET enabled=:enabled, body=:body,\n              enabled_at=CASE WHEN :enabled AND NOT enabled THEN NOW() WHEN NOT :enabled THEN NULL ELSE enabled_at END
            WHERE trigger=:trigger
        """), {"trigger": trigger, "enabled": enabled, "body": body.strip()})
        await s.commit()

async def enqueue_reminders():
    """At-most-once reminder per user and rule, no replay of old registrations.

    Only users whose qualifying event happened after rule activation are eligible.
    A previously recorded intent skips the reminder.
    """
    async with SessionLocal() as s:
        async with s.begin():
            for trigger in TRIGGERS:
                await s.execute(text("""
                    WITH eligible AS (
                        SELECT u.telegram_id, r.body
                        FROM users u JOIN reminder_rules r ON r.trigger=:trigger
                        WHERE r.enabled
                          AND NOT EXISTS (SELECT 1 FROM broadcast_opt_out o WHERE o.telegram_id=u.telegram_id)
                          AND NOT EXISTS (
                              SELECT 1 FROM reminder_deliveries d
                              WHERE d.trigger=:trigger AND d.telegram_id=u.telegram_id
                          )
                          AND (
                            (:trigger='no_offer_24h'
                             AND u.created_at >= r.enabled_at
                             AND u.created_at <= NOW() - INTERVAL '24 hours'
                             AND NOT EXISTS (SELECT 1 FROM offer_acceptances a WHERE a.telegram_id=u.telegram_id))
                            OR
                            (:trigger='no_click_24h'
                             AND EXISTS (SELECT 1 FROM offer_acceptances a
                                         WHERE a.telegram_id=u.telegram_id
                                           AND a.accepted_at <= NOW() - INTERVAL '24 hours'
                                           AND a.accepted_at >= r.enabled_at)
                             AND NOT EXISTS (SELECT 1 FROM subscription_clicks c WHERE c.telegram_id=u.telegram_id))
                          )
                        ORDER BY u.telegram_id LIMIT 100
                    ), marked AS (
                        INSERT INTO reminder_deliveries (trigger, telegram_id)
                        SELECT :trigger, telegram_id FROM eligible
                        ON CONFLICT DO NOTHING RETURNING telegram_id
                    ), campaigns AS (
                        INSERT INTO broadcasts (body, audience, campaign_kind, recipient_id, status)
                        SELECT e.body, 'started', 'reminder:' || :trigger, e.telegram_id, 'queued'
                        FROM eligible e JOIN marked m USING (telegram_id)
                        RETURNING id, recipient_id
                    )
                    INSERT INTO broadcast_recipients (broadcast_id, telegram_id)
                    SELECT id, recipient_id FROM campaigns
                """), {"trigger": trigger})

async def render_personalized(body, first_name):
    safe = (first_name or "").strip()
    # No HTML/Markdown parse mode: names are plain text.
    return body.replace("{first_name}", safe or "друг").replace("{link}", "")
