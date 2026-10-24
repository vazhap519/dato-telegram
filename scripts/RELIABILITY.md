# Защита данных и мониторинг «Круг Души»

## Установка (один раз после обычного обновления GitHub/Vultr)

```bash
sudo bash /opt/dato-telegram/scripts/install_reliability.sh
```

Установщик **сначала** выполняет первую копию БД и проверяет её формат, затем включает системные таймеры. Если копия не создаётся, установка остановится. Не отключает и не перенастраивает рабочие службы бота.

- Архивы PostgreSQL: `/var/backups/dato-telegram/dato-YYYYMMDDTHHMMSSZ.dump`. Директория 0700, файлы 0600.
- Сохранение: последние 14 успешных архивов.
- Копия: ежедневно около 02:20 UTC (06:20 по Тбилиси, плюс до пяти минут).
- Проверка: примерно каждые 15 минут после запуска сервера.
- Мониторинг: `pg_isready`, HTTPS страницы админки, возраст архива и проверка читаемости формата.
- Дополнительно: проверка конкретных systemd-служб после внесения их имён в конфигурацию.
- Сервисные ошибки: `journalctl -u dato-postgres-backup.service` и `journalctl -u dato-health-check.service`.
- Состояние уведомлений: `/var/lib/dato-reliability/last_alert.json`.

## Подключение тревог в Telegram

Бот должен иметь открытый личный чат с администратором (пользователь должен первым нажать /start).

```bash
sudo nano /etc/dato-reliability.conf
```

Заполнить `DATO_ALERT_CHAT_ID=<ваш числовой Telegram ID>` (не токен). 
По желанию — `DATO_MONITOR_SERVICES=dato-bot.service,dato-admin.service`, **только если названия реальных служб совпадают**.
Реальные службы можно посмотреть:
```bash
systemctl list-units --type=service --all --no-pager | grep -Ei 'dato|telegram|uvicorn|bot'
```

После изменения конфига изменения подхватываются при следующем запуске службы (перезапуск бота не требуется).
Изначально без `DATO_ALERT_CHAT_ID` проверки работают, но Telegram-уведомления выключены.

## Ручное тестирование

```bash
sudo systemctl start dato-postgres-backup.service
sudo journalctl -u dato-postgres-backup.service -n 20 --no-pager
sudo /usr/bin/python3 /opt/dato-telegram/scripts/reliability.py verify
sudo /usr/bin/python3 /opt/dato-telegram/scripts/reliability.py restore-test
sudo systemctl start dato-health-check.service
sudo journalctl -u dato-health-check.service -n 20 --no-pager
systemctl list-timers 'dato-*'
```

`verify` проверяет формат; **не является проверкой полноценного восстановления**.

`restore-test` создаёт временную БД с случайным именем `dato_restore_test_...`, выполняет `pg_restore`, проверяет наличие таблиц и затем удаляет только временную БД. Никаких изменений в рабочей базе. Для этой команды пользователь PostgreSQL должен иметь `CREATEDB`, а установленные расширения должны быть доступны. Ошибка `permission denied to create database` означает нехватку прав для теста, а не повреждение архива. При запуске вручную команда использует `DATABASE_URL` из `.env`.

**Не запускайте восстановление архива непосредственно в рабочую базу** без отдельного плана миграции и проверки.

## Ограничения

Архивы на том же VPS **не защищают от полной потери сервера или диска**. Для полноценной отказоустойчивости нужно добавить шифрованное внешнее хранилище/снимки Vultr и протестировать восстановление вне сервера. Настоящий restore-test может требовать отдельных PostgreSQL прав. Настройки проверок не контролируют каждое отдельное исключение внутри приложения; для анализа Python-сбоев используйте журналы приложений и установите `DATO_MONITOR_SERVICES`.
