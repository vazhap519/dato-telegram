# Blue/green обновления — безопасный план внедрения

## Текущее состояние

Сейчас обновления выполняет локальный скрипт
`/usr/local/bin/update-dato-telegram`, который не хранится в репозитории.
Админка обслуживается `dato-telegram-admin.service` на `127.0.0.1:8081`.
Перезапуск вызывает кратковременный `502` до завершения startup.

**Важно:** GitHub не содержит актуальную конфигурацию этого systemd-сервиса
или полный код действующего обновляющего скрипта. Поэтому слепая установка
нового деплоя небезопасна.

## Этап 0 — только проверка, без влияния на рабочие сервисы

После обновления GitHub:

```bash
/usr/bin/python3 /opt/dato-telegram/scripts/deploy_readiness.py
```

Скрипт выводит неперсональные сведения о точках запуска, текущем
маршрутизировании, структуре обновления и ресурсах. Он не изменяет файлы,
не читает .env, не перезапускает службы, не делает запросы к Telegram.

## План после проверки состояния

1. Добавить отдельные директории релизов и immutable исходники для админки.
2. Сохранить секреты в прежнем root-owned конфиге; не копировать их в Git.
3. Запустить резервный Uvicorn на `127.0.0.1:8082`, при этом текущий
   `8081` продолжает работать.
4. Проверить на резервном экземпляре GET `/login` (или другой
   лёгкий read-only endpoint) и GET `/tg-support`, а также необходимые
   зависимости. Не выполнять реальных оплат/писем/групповых действий.
5. Переключать Nginx только после успешных health checks, сохраняя прежнее
   конфигурирование. Переключение делать атомарным и с rollback.
6. Оставлять прежний экземпляр активным на время проверки нового.
7. Не запускать второй экземпляр *бота*: Telegram polling должен быть один.
8. Внести проверенные скрипты обновления, Nginx шаблонов и systemd units
   в репозиторий; установка на сервер остаётся явным действием администратора.

## Ограничения

Текущая админка выполняет CREATE/ALTER TABLE и инициализации при startup.
Два экземпляра могут работать на одной PostgreSQL только после аудита
совместимости схемы и миграций. Blue/green не решает миграции с удалением
или изменением старых столбцов. Перед переключением нужна резервная копия,
предварительная проверка и обратимая миграция.

Обновляющий скрипт в данный момент изменяет исходники на месте; нельзя
просто запустить два Uvicorn из того же каталога и считать это двумя
изолированными релизами. Нужны независимые release-директории, либо
другой механизм неизменяемого снимка кода.

Деплой автоматически не включается, пока не известны реальные
`ExecStart`, `WorkingDirectory` и команды текущего обновляющего скрипта.

## Этап 1 — проверка параллельного экземпляра

На VPS с 949 MiB RAM текущие значения: админка ~100 MiB, бот ~52 MiB,
MemAvailable ~395 MiB, swap ~2.3 GiB. Это **не** гарантия достаточности RAM
под двойную нагрузку; скрипт отказывается запускаться при MemAvailable <250 MiB.

Обновить только файлы проверочного скрипта из GitHub, без обновления
работающих Python-служб:

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/bluegreen_probe.py > /root/dato-bluegreen-probe.py
python3 -m py_compile /root/dato-bluegreen-probe.py
```

Команда для пробного запуска (вместо зависимости от root Python используем
виртуальное окружение действующего проекта):

```bash
/opt/dato-telegram/.venv/bin/python /root/dato-bluegreen-probe.py --confirm
```

Проверка извлекает версию `origin/main` в отдельный каталог,
поднимает **только второй экземпляр FastAPI на 8082**, ждёт ответов `/login`
и `/tg-support` и гарантированно завершает пробный процесс.
Не переключает Nginx, не перезапускает бот и не меняет конфигурацию systemd.
**При старте FastAPI выполняются имеющиеся инициализации БД** — это
не строго read-only процедура на уровне PostgreSQL. Сначала нужны
актуальные резервные копии.

Отчёт о прохождении проверки нужен перед этапом 2: долгоживущие slot units,
атомарное переключение Nginx и обратимый deploy script.
Нельзя объявлять zero downtime установленным после одного probe.

## Этап 2 — постоянный standby, затем ручное переключение

После успешного пробного запуска (проверен commit
`da4a95e1853947cae798d5193bdb48e7c90693ea`, `8082`, ~9 секунд)
подготовлен `scripts/admin_bluegreen.py`. В отличие от пробного теста,
он создаёт постоянный systemd-сервис резервной админки. Текущая версия
всё ещё отвечает на `8081`; Telegram polling бот не затрагивается.

**Сначала только ставим standby и проверяем его:**

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/admin_bluegreen.py > /root/dato-admin-bluegreen.py
/opt/dato-telegram/.venv/bin/python -m py_compile /root/dato-admin-bluegreen.py
sudo /opt/dato-telegram/.venv/bin/python /root/dato-admin-bluegreen.py status
sudo /opt/dato-telegram/.venv/bin/python /root/dato-admin-bluegreen.py stage da4a95e1853947cae798d5193bdb48e7c90693ea
sudo /opt/dato-telegram/.venv/bin/python /root/dato-admin-bluegreen.py status
```

Ожидается: `8081 healthy True`, `8082 healthy True`, `Nginx: 8081`.
**После этого остановиться и проверить журналы, прежде чем переключать.**

Дальнейший **отдельный**, вручную подтверждённый шаг:

```bash
sudo /opt/dato-telegram/.venv/bin/python /root/dato-admin-bluegreen.py switch
```

Команда переключает четыре `proxy_pass` только в конфиге
`/etc/nginx/sites-enabled/dato-telegram`, после backup и проверки;
действующий `8081` не остановится. Если `nginx -t`, reload либо
проверка публичной Mini App не проходит, конфигурация автоматически
возвращается. Ручной откат:

```bash
sudo /opt/dato-telegram/.venv/bin/python /root/dato-admin-bluegreen.py rollback
```

Ограничения этапа 2: это **первое** управляемое переключение, ещё не
полностью автоматический релизный цикл. Нынешний
`/usr/local/bin/update-dato-telegram` продолжает обновлять
`/opt/dato-telegram` на месте и перезапускать обе старые службы.
Его нельзя использовать как окончательное blue/green-обновление:
после переключения на standby также нужен новый установщик, который
будет создавать следующий релиз в свободном слоте. Не отключать старую
службу, пока новая не проверена под реальными запросами.

Ни один из приведённых шагов не запускает второй Telegram polling.
При обновлениях, меняющих схему БД, требуется отдельная проверка
совместимости обеих версий.

## Этап 3 — независимые релизы вместо изменения файлов на месте

Действующий обновляющий скрипт (проверен на VPS):
- сохраняет архив исходников в `/root/dato-telegram-deploy-backups`;
- выполняет `git merge --ff-only origin/main` в рабочем `/opt/dato-telegram`;
- выполняет `pip install` в **общем** `.venv`;
- перезапускает **обе** службы `dato-telegram-bot`, `dato-telegram-admin`.

Этот сценарий **не использовать** во время перехода на релизные слоты,
поскольку он меняет рабочий экземпляр на месте.

В GitHub добавлены:
- `scripts/slot_runner.py`: автономный запуск FastAPI из отдельного
  неизменяемого снимка Git с собственной `.venv`, порт 8081 или 8082.
- `scripts/deploy_admin_bluegreen.py`: команды `plan`, `stage --commit`,
  `cutover`. **Новый скрипт пока не установлен вместо старого.**

Первая проверка после `git fetch origin main`, **без изменения системы**:

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/deploy_admin_bluegreen.py > /root/dato-deploy-admin-v2.py
/opt/dato-telegram/.venv/bin/python -m py_compile /root/dato-deploy-admin-v2.py
/opt/dato-telegram/.venv/bin/python /root/dato-deploy-admin-v2.py plan
```

Что произойдёт на `stage --commit SHA` после проверки: скрипт
архивирует нужный commit из Git в отдельный каталог
`/opt/dato-admin-releases/SHA`, устанавливает зависимости в его
`.venv`, проверяет исходники и лишь затем останавливает **неактивную**
админку на свободном порту. Создаёт либо заменяет только сервис свободного
слота, запускает его и проверяет `/login` и `/tg-support`. При
неудаче пытается вернуть прежний systemd-сервис. **Активный порт и
конфигурация Nginx остаются нетронутыми.**

Команда `cutover` после успешного stage в отдельном действии сохраняет
конфиг Nginx, проверяет *оба* порта, переключает четыре прокси на новый
порт, делает `nginx -t`, reload и публичную HTTP-проверку, с
автоматическим возвратом конфига при ошибке.

Ограничения: `stage` консервативно отказывается от релизов, где
поменялись `db/`, `bot/` или `requirements.txt`, относительно HEAD
старого рабочего каталога. Такие обновления требуют отдельного
плана совместимости, деплоя бота и возможных миграций БД. Никакого
автоматического перезапуска polling-бота этот админский деплой не делает.
Обновление Python-пакетов выполняется только в release-venv, не в
активном `.venv`.

После первого полного деплоя и внешнего тестирования можно будет
переопределить старую команду `update-dato-telegram` через
специальный установщик и отдельное обновление бота. **Пока
старый установщик не переустанавливать и не запускать.**

## Этап 4 — автозапуск обоих слотов после перезагрузки

Фактическое состояние после первого cutover:
- Nginx = 8081, blue healthy (managed), standby 8082 healthy;
- `dato-telegram-admin.service` enabled/inactive (опасный конфликт за 8081 на reboot);
- `dato-telegram-admin-blue.service` disabled/active;
- `dato-telegram-admin-standby.service` disabled/active;
- бот enabled/active.

Подготовлен `scripts/enable_admin_bluegreen_boot.py`: проверяет четыре
маршрута Nginx, оба порта, два соответствующих unit-файла и состояние
старого сервиса. После preflight **без запуска/остановки сервисов**
выполняет `systemctl disable dato-telegram-admin.service`,
`systemctl enable dato-telegram-admin-blue.service`,
`systemctl enable dato-telegram-admin-standby.service`.
При ошибке пытается восстановить прежние флаги автозапуска.
Бот и Nginx не изменяет. Сервер не перезагружает.

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/enable_admin_bluegreen_boot.py > /root/dato-enable-bluegreen-boot.py
/opt/dato-telegram/.venv/bin/python -m py_compile /root/dato-enable-bluegreen-boot.py
sudo /opt/dato-telegram/.venv/bin/python /root/dato-enable-bluegreen-boot.py
```

Проверка состояния:
```bash
systemctl is-enabled dato-telegram-admin.service
systemctl is-enabled dato-telegram-admin-blue.service
systemctl is-enabled dato-telegram-admin-standby.service
systemctl is-enabled dato-telegram-bot.service
```

Ожидаемые значения: `disabled`, `enabled`, `enabled`, `enabled`.
Не проводить тестовую перезагрузку без резервной копии, мониторинга и
проверки расписания: одновременно стартующие админки на VPS <1GiB RAM
создают пиковую нагрузку. При необходимости добавить последовательный
запуск и дополнительные проверки готовности.

## Этап 5 — отдельная единая команда обновления админки

Файлы в GitHub:
- `scripts/update_admin_safe.py` — оркестрация fetch → stage → cutover →
  проверки, автоматический возврат Nginx на прежний порт при проблемах,
  отдельный блокирующий flock, root-only JSONL-журнал
  `/var/log/dato-deploy/admin-updates.jsonl`.
- `scripts/install_admin_updater.py` — берёт **два проверенных скрипта**
  из `origin/main`, проверяет синтаксис и устанавливает независимую
  команду `/usr/local/bin/update-dato-admin`. Существующая
  `/usr/local/bin/update-dato-telegram` **не изменяется**.
  Существующие службы и Nginx **не перезапускаются при установке**.

Установка с предварительной проверкой (запускать последовательно):

```bash
cd /opt/dato-telegram
git fetch origin main
git show origin/main:scripts/install_admin_updater.py > /root/install-dato-admin-updater.py
/opt/dato-telegram/.venv/bin/python -m py_compile /root/install-dato-admin-updater.py
/opt/dato-telegram/.venv/bin/python /root/install-dato-admin-updater.py
/opt/dato-telegram/.venv/bin/python /root/install-dato-admin-updater.py --install
update-dato-admin check
```

После подтверждения `check` новая единая команда:

```bash
update-dato-admin deploy
```

Она:
1. Убедится в работоспособности активного экземпляра и автозапуске слотов.
2. Обновит только git refs (`git fetch`) без изменения текущих исходников.
3. Сравнит коммит целевого релиза с реально работающим; одинаковый →
   сообщение об отсутствии обновлений.
4. Откажется от опасных изменений `db/`, `bot/`, `requirements.txt`,
   расхождения истории Git и грязного рабочего дерева.
5. Создаст релиз из Git в отдельном каталоге, поставит пакеты в его venv,
   заменит только неактивный systemd-слот.
6. Протестирует оба экземпляра, переключит четыре upstream Nginx, проверит
   публичную Mini App и статус Telegram-бота.
7. При неудаче после переключения вернёт Nginx на прежний порт.
8. Запишет начало, завершение и ошибку в приватный JSONL-журнал.

Диагностика и ручной возврат:

```bash
update-dato-admin check
update-dato-admin history
update-dato-admin rollback
```

Ограничения: это бесшовное переключение **только FastAPI-админки**.
Два слота имеют общую PostgreSQL — destructive migrations и изменения
бота требуют отдельного согласованного релиза. Автоматическое
обновление и перезапуск самого polling-бота пока не реализованы, старый
комплексный установщик запрещён к использованию после перехода на
слоты. HTTP-проверки не заменяют тесты бизнес-логики, обновление вебхуков
или реальных действий пользователей. Переход на новую версию не означает
абсолютную гарантию нулевых ошибок, но старый экземпляр остаётся
доступен для отката.
