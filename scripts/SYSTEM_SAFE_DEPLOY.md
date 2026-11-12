# Согласованное обновление всей Telegram-системы

После успешных проверок отдельных процессов (админка Blue/Green, Telegram
polling-бот) добавлен **независимый** установщик
`scripts/install_system_updater.py` и новый инструмент
`scripts/update_system_safe.py`.

Действующие команды `update-dato-admin`, `update-dato-bot` сохраняются.
Устаревшую `update-dato-telegram` **НЕ ИСПОЛЬЗОВАТЬ**.

## Принцип

У бота и обеих админок общая PostgreSQL. Поэтому единый инструмент
обновляет обе службы на **один и тот же commit origin/main**, но по
разным технологиям:

1. Без остановки служб: fetch, проверка версий и совместимости, RAM,
   Git history, доступности двух слотов, отсутствие второго poller.
2. Подготовка обоих независимых релизных окружений: admin `.venv`,
   bot `.bot-venv`. При подготовке не меняются systemd/Nginx.
3. **Непосредственно перед обновлением** создаётся новый custom-format
   PostgreSQL-архив через `scripts/reliability.py backup` и
   проверяется его manifest. Архив читается, но это **не restore-test**.
4. Новая админка запускается на свободном порту (8081/8082), проходит
   проверку, Nginx переключается на неё с сохранением работающего
   старого экземпляра.
5. Только после проверки админки останавливается старый polling-бот,
   проверяется отсутствие второго polling-процесса и запускается новый.
   Бот может кратковременно не обрабатывать входящие updates.
6. Проверяются PID, журнал aiogram polling, HTTP обеих админок и
   публичная Mini App; запись в приватный audit log.
7. В случае неудачи после переключения выполняется **попытка**
   восстановления прежней конфигурации бота и Nginx. Автоматический
   откат не отменяет возможные изменения данных в общей PostgreSQL.

Время простоя бота не гарантируется нулевым, HTTP-тесты не гарантируют
успех всех бизнес-операций.

## Совместимость БД — строгая граница

Если между **любым** из текущих коммитов (бот, активная админка и
рабочее Git-дерево) и целевым есть изменения:
`db/`, `migrations/`, `alembic/`, `database/`, `*.sql`,
`requirements.txt` или lockfiles зависимостей, **единый deploy
автоматически блокируется**.

Это предупреждение, а не автоматический механизм миграций. Для этих
обновлений нужны ручной анализ обратной совместимости, миграция по
отдельному сценарию и тест восстановления БД. Любая версия приложения
может менять схему PostgreSQL своими startup-функциями; file diff
не заменяет экспертизу и реальные integration-тесты.

Не пытаться обходить блокировку удалением файлов или искусственным
сдвигом Git HEAD.

## Установка (без перезапуска)

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/install_system_updater.py > /root/install-dato-system-updater.py
/opt/dato-telegram/.venv/bin/python -m py_compile /root/install-dato-system-updater.py
/opt/dato-telegram/.venv/bin/python /root/install-dato-system-updater.py
/opt/dato-telegram/.venv/bin/python /root/install-dato-system-updater.py --install
update-dato-system check
```

`check` не выполняет Git fetch и ничего не изменяет.

После получения и проверки отчёта:

```bash
update-dato-system prepare
```

`prepare` делает fetch и подготавливает только отдельные окружения
(без перезапуска служб), может занять ресурсы диска и памяти.

### Запуск совместного релиза

После проверки `prepare`, свежести базы и отсутствия рискованных
изменений:

```bash
update-dato-system deploy
```

Для диагностики:

```bash
update-dato-system check
update-dato-system history
update-dato-admin check
update-dato-bot check
systemctl is-active dato-telegram-bot.service
```

Журнал: `/var/log/dato-deploy/system-updates.jsonl`.

### Важные оговорки

- Новая команда не устанавливается поверх существующих updater-скриптов.
- При одновременном запуске отдельных admin/bot обновлений общий lock
  `/run/lock/dato-admin-bluegreen.lock` предотвращает конфликт.
- В одиночной подготовке `prepare` существующие релизные каталоги
  не удаляются. На маленьком VPS нужно следить за доступным RAM/disk.
- Backup производится до первого переключения, не до `prepare`.
- Публичные webhook-маршруты Nginx переключаются вместе с админкой.
- Если автоматическое восстановление не сработало, проверять конкретно
  Nginx, оба admin slots, bot MainPID/polling и журнал; не запускать
  слепо повторный deploy.
- **Автоматическая очистка старых релизов, рискованные миграции и
  полностью безостановочное переключение polling-бота не реализованы.**
