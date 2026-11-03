"""Conservative moderation of Telegram service messages in one configured group.

No filtering of ordinary user content and no changes to subscription membership.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

FIELDS = ("join_enabled", "leave_enabled", "pin_enabled", "other_enabled")

async def initialize_group_moderation():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_settings (
                id INTEGER PRIMARY KEY CHECK (id=1),
                join_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                leave_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                pin_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                other_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                updated_by TEXT NOT NULL DEFAULT 'system',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        for column, kind in (
            ("content_enabled","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("block_links","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("block_invites","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("block_forwards","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("block_flood","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("block_duplicates","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("flood_limit","INTEGER NOT NULL DEFAULT 6"),
            ("flood_window_seconds","INTEGER NOT NULL DEFAULT 10"),
            ("duplicate_window_seconds","INTEGER NOT NULL DEFAULT 90"),
            ("warning_limit","INTEGER NOT NULL DEFAULT 3"),
            ("mute_minutes","INTEGER NOT NULL DEFAULT 0"),
            ("notify_user","BOOLEAN NOT NULL DEFAULT TRUE"),
            ("allowed_domains","TEXT NOT NULL DEFAULT ''"),
            ("blocked_words","TEXT NOT NULL DEFAULT ''"),
            ("block_media","BOOLEAN NOT NULL DEFAULT FALSE"),
            ("trusted_users","TEXT NOT NULL DEFAULT ''"),
            ("observe_only","BOOLEAN NOT NULL DEFAULT TRUE")
        ):
            await c.execute(text(
                "ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS " + column + " " + kind
            ))
        await c.execute(text("""
            INSERT INTO group_moderation_settings(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_statistics (
                id INTEGER PRIMARY KEY CHECK (id=1),
                deleted_total BIGINT NOT NULL DEFAULT 0,
                failed_total BIGINT NOT NULL DEFAULT 0,
                last_deleted_at TIMESTAMPTZ
            )
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_violations (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                telegram_id BIGINT NOT NULL,
                message_id BIGINT,
                reason TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_mod_violations_user
            ON group_moderation_violations(chat_id,telegram_id,created_at DESC)
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_manual_actions (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                telegram_id BIGINT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                duration_minutes INTEGER,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error_detail TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_group_manual_user
            ON group_moderation_manual_actions(chat_id,telegram_id,created_at DESC)
        """))
        await c.execute(text("""
            INSERT INTO group_moderation_statistics(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))

async def get_moderation_settings():
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT join_enabled,leave_enabled,pin_enabled,other_enabled,
                   content_enabled,block_links,block_invites,block_forwards,
                   block_flood,block_duplicates,flood_limit,flood_window_seconds,
                   duplicate_window_seconds,warning_limit,mute_minutes,notify_user,
                   allowed_domains,blocked_words,block_media,trusted_users,observe_only,
                   updated_by,updated_at
            FROM group_moderation_settings WHERE id=1
        """))).mappings().one()
        return dict(row)

async def save_moderation_settings(join_enabled,leave_enabled,pin_enabled,
                                   other_enabled,actor):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE group_moderation_settings
                SET join_enabled=:joined,leave_enabled=:left,pin_enabled=:pinned,
                    other_enabled=:other,updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """),{"joined":join_enabled,"left":leave_enabled,
                  "pinned":pin_enabled,"other":other_enabled,"actor":actor})

async def moderation_stats():
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT deleted_total,failed_total,last_deleted_at
            FROM group_moderation_statistics WHERE id=1
        """))).mappings().one()
        return dict(row)

async def count_moderation_action(success):
    async with SessionLocal() as s:
        async with s.begin():
            if success:
                await s.execute(text("""
                    UPDATE group_moderation_statistics
                    SET deleted_total=deleted_total+1,last_deleted_at=NOW() WHERE id=1
                """))
            else:
                await s.execute(text("""
                    UPDATE group_moderation_statistics
                    SET failed_total=failed_total+1 WHERE id=1
                """))


CONTENT_OPTIONS = ("content_enabled","block_links","block_invites","block_forwards",
                   "block_flood","block_duplicates","notify_user","block_media","observe_only")
CONTENT_RANGES = {
    "flood_limit":(3,20),
    "flood_window_seconds":(5,120),
    "duplicate_window_seconds":(15,600),
    "warning_limit":(1,10),
    "mute_minutes":(0,1440),
}

async def save_content_settings(values,actor):
    data={}
    for key in CONTENT_OPTIONS:
        data[key]=bool(values.get(key,False))
    for key,(lo,hi) in CONTENT_RANGES.items():
        value=int(values[key])
        if not lo <= value <= hi:
            raise ValueError(f"Неверное значение {key}: допустимо {lo}–{hi}")
        data[key]=value
    for key, max_chars in (("allowed_domains",1200),("blocked_words",1200),("trusted_users",1500)):
        val=str(values.get(key,"") or "").strip()
        if len(val)>max_chars:
            raise ValueError(f"Слишком длинный список {key}")
        if key=="trusted_users":
            import re
            members=[x.strip() for x in val.replace(",","\n").splitlines() if x.strip()]
            if len(members)>100 or any(not re.fullmatch(r"[1-9][0-9]{0,12}",u) for u in members):
                raise ValueError("Доверенные участники: не более 100 корректных Telegram ID")
        data[key]=val
    data["actor"]=actor
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE group_moderation_settings SET
                    content_enabled=:content_enabled,block_links=:block_links,
                    block_invites=:block_invites,block_forwards=:block_forwards,
                    block_flood=:block_flood,block_duplicates=:block_duplicates,
                    flood_limit=:flood_limit,flood_window_seconds=:flood_window_seconds,
                    duplicate_window_seconds=:duplicate_window_seconds,
                    warning_limit=:warning_limit,mute_minutes=:mute_minutes,
                    notify_user=:notify_user,block_media=:block_media,
                    allowed_domains=:allowed_domains,blocked_words=:blocked_words,
                    trusted_users=:trusted_users,observe_only=:observe_only,
                    updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """),data)

async def log_violation(chat_id,uid,message_id,reason,action):
    """Record action and count violations within a rolling 24-hour window."""
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                INSERT INTO group_moderation_violations
                  (chat_id,telegram_id,message_id,reason,action)
                VALUES (:chat,:uid,:mid,:reason,:action)
            """),{"chat":chat_id,"uid":uid,"mid":message_id,
                  "reason":reason,"action":action})
            return (await s.execute(text("""
                SELECT count(*) FROM group_moderation_violations
                WHERE chat_id=:chat AND telegram_id=:uid
                  AND created_at>NOW()-INTERVAL '24 hours'
                  AND action='deleted'
                  AND created_at>COALESCE((
                      SELECT MAX(created_at) FROM group_moderation_violations
                      WHERE chat_id=:chat AND telegram_id=:uid AND action='reset'
                  ),'-infinity'::timestamptz)
            """),{"chat":chat_id,"uid":uid})).scalar_one()

async def moderation_history(limit=60):
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT chat_id,telegram_id,message_id,reason,action,created_at
            FROM group_moderation_violations ORDER BY id DESC LIMIT :limit
        """),{"limit":min(100,max(1,int(limit)))})).mappings().all()
        return [dict(row) for row in rows]

async def moderation_summary():
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT count(*) FILTER (WHERE action='deleted') deleted,
                   count(*) FILTER (WHERE action='muted') muted,
                   count(*) FILTER (WHERE action='failed') failed,
                   count(DISTINCT telegram_id) FILTER (WHERE created_at>NOW()-INTERVAL '7 days') users_7d
            FROM group_moderation_violations
        """))).mappings().one()
        return dict(row)

async def clear_user_warnings(chat_id,uid):
    async with SessionLocal() as s:
        async with s.begin():
            # Don't destroy moderation history. Reset the rolling counter via
            # a separate reset marker, so past actions are still auditable.
            await s.execute(text("""
                INSERT INTO group_moderation_violations
                 (chat_id,telegram_id,reason,action)
                VALUES (:chat,:uid,'admin_reset','reset')
            """),{"chat":chat_id,"uid":uid})



async def moderation_offenders(chat_id, search="", page=1):
    """Read-only summary based on recorded anti-spam violations."""
    page=max(1,min(10000,int(page)))
    q=(search or "").strip().lstrip("@")[:80]
    async with SessionLocal() as s:
        params={"chat":chat_id,"q":q,"offset":(page-1)*25}
        base="""
            FROM group_moderation_violations v
            LEFT JOIN users u ON u.telegram_id=v.telegram_id
            WHERE v.chat_id=:chat AND v.action IN ('deleted','muted','observed','failed')
              AND (CAST(:q AS TEXT)='' OR CAST(v.telegram_id AS TEXT) ILIKE '%'||:q||'%'
                OR COALESCE(u.username,'') ILIKE '%'||:q||'%'
                OR COALESCE(u.first_name,'') ILIKE '%'||:q||'%')
        """
        total=(await s.execute(text("SELECT COUNT(DISTINCT v.telegram_id) "+base),params)).scalar_one()
        rows=(await s.execute(text("""
            SELECT v.telegram_id,MAX(u.username) username,
                   MAX(u.first_name) first_name,
                   COUNT(*) FILTER (WHERE v.action='deleted') deletions,
                   COUNT(*) FILTER (WHERE v.action='observed') observations,
                   COUNT(*) FILTER (WHERE v.action='muted') restrictions,
                   MAX(v.created_at) latest_at
        """+base+"""
            GROUP BY v.telegram_id
            ORDER BY latest_at DESC LIMIT 25 OFFSET :offset
        """),params)).mappings().all()
        return [dict(row) for row in rows],total

async def moderation_user_details(chat_id,telegram_id):
    async with SessionLocal() as s:
        person=(await s.execute(text("""
            SELECT telegram_id,username,first_name,last_name
            FROM users WHERE telegram_id=:uid
        """),{"uid":telegram_id})).mappings().first()
        incidents=(await s.execute(text("""
            SELECT reason,action,message_id,created_at
            FROM group_moderation_violations
            WHERE chat_id=:chat AND telegram_id=:uid
            ORDER BY id DESC LIMIT 75
        """),{"chat":chat_id,"uid":telegram_id})).mappings().all()
        manual=(await s.execute(text("""
            SELECT actor,action,duration_minutes,details,status,error_detail,created_at
            FROM group_moderation_manual_actions
            WHERE chat_id=:chat AND telegram_id=:uid
            ORDER BY id DESC LIMIT 50
        """),{"chat":chat_id,"uid":telegram_id})).mappings().all()
        current=(await s.execute(text("""
            SELECT COUNT(*) FROM group_moderation_violations v
            WHERE chat_id=:chat AND telegram_id=:uid
              AND action='deleted'
              AND created_at>NOW()-INTERVAL '24 hours'
              AND created_at>COALESCE((
                SELECT MAX(created_at) FROM group_moderation_violations
                WHERE chat_id=:chat AND telegram_id=:uid AND action='reset'
              ),'-infinity'::timestamptz)
        """),{"chat":chat_id,"uid":telegram_id})).scalar_one()
        return {
            "person":dict(person) if person else None,
            "incidents":[dict(x) for x in incidents],
            "manual":[dict(x) for x in manual],
            "warnings":current,
        }

async def record_manual_action(chat_id,telegram_id,actor,action,status,
                               duration_minutes=None,details="",error_detail=None):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                INSERT INTO group_moderation_manual_actions
                  (chat_id,telegram_id,actor,action,status,duration_minutes,details,error_detail)
                VALUES (:chat,:uid,:actor,:action,:status,:duration,:details,:error)
            """),{"chat":chat_id,"uid":telegram_id,"actor":actor,"action":action,
                  "status":status,"duration":duration_minutes,
                  "details":(details or "")[:500],
                  "error":(error_detail or "")[:300] if error_detail else None})

async def moderation_manual_history(chat_id,limit=60):
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT m.telegram_id,m.actor,m.action,m.status,m.duration_minutes,
                   m.details,m.created_at,u.username
            FROM group_moderation_manual_actions m
            LEFT JOIN users u ON u.telegram_id=m.telegram_id
            WHERE m.chat_id=:chat ORDER BY m.id DESC LIMIT :limit
        """),{"chat":chat_id,"limit":max(1,min(int(limit),100))})).mappings().all()
        return [dict(x) for x in rows]

async def set_trusted_user(uid,enabled,actor):
    if not 0 < uid <= 9999999999999:
        raise ValueError("Недопустимый Telegram ID")
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
                SELECT trusted_users FROM group_moderation_settings
                WHERE id=1 FOR UPDATE
            """))).mappings().one()
            members=set(x.strip() for x in (row["trusted_users"] or "").replace(",","\n").splitlines() if x.strip())
            if enabled:
                members.add(str(uid))
            else:
                members.discard(str(uid))
            if len(members)>100:
                raise ValueError("Лимит: 100 доверенных участников")
            await s.execute(text("""
                UPDATE group_moderation_settings SET trusted_users=:members,
                       updated_by=:actor,updated_at=NOW() WHERE id=1
            """),{"members":"\n".join(sorted(members,key=int)),"actor":actor})
