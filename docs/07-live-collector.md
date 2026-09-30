# Live collector с октября 2026

## Назначение

Начиная с октября 2026 guardamar-digest-bot сам накапливает новые и
отредактированные сообщения основной группы через Telegram Bot API. Ручной
Telegram Desktop JSON остаётся аварийным fallback и способом импортировать
исторический сентябрь 2026.

Collector не классифицирует сообщения, не вызывает LLM и ничего не публикует.
Его задача только сохранить сырой text/caption и Telegram metadata в
существующие таблицы messages и entries.

## Команды

Однократный сбор ожидающих updates:

~~~bash
guardamar-digest collect
~~~

Состояние collector:

~~~bash
guardamar-digest collector-status
~~~

## Что сохраняется

Collector принимает только message и edited_message из TELEGRAM_SOURCE_CHAT_ID.
Сохраняются message_id, дата публикации в Europe/Madrid, immutable sender ID,
text/caption, публичная ссылка, тип media и media_group_id.

Сообщения других чатов, обычные сообщения от ботов и записи без text/caption
не создают digest entries. Если у существующего сообщения при редактировании
text/caption полностью удалён, запись исключается; если текст позже вернётся,
запись восстанавливается.

## Offset и идемпотентность

Telegram update offset хранится в той же SQLite в таблице collector_state.
Offset продвигается только после успешного сохранения полученной пачки updates.
Повторная доставка безопасна благодаря UNIQUE(chat_id, message_id).

## Контроль полноты

Collector запускается каждые 10 минут через cron. coverage_status становится
uncertain, если между успешными запусками прошло 23 часа или больше. Это
состояние намеренно липкое: такой месяц нельзя автоматически считать
гарантированно полным. Первый запуск считается покрывающим начало месяца только
если он произошёл в первые 23 часа локального месяца.

При coverage_status=uncertain JSON export используется как аварийный источник
восстановления или проверки полноты.

## Ограничение удалений

Telegram Bot API сообщает новые и отредактированные обычные сообщения, но не
предоставляет универсальный update для удаления обычного пользовательского
сообщения группы. Поэтому collector не гарантирует немедленное обнаружение
удалённого автором объявления. MTProto/user session ради этого сейчас не
добавляется; при необходимости recovery будет спроектирован отдельно.

## Production layout

~~~text
~/bots/guardamar-digest
~/bots/guardamar-digest/state/digest.sqlite3
~~~

Deployment script подставляет реальный абсолютный путь до
.venv/bin/guardamar-digest и не полагается на cron PATH.

## Требования Telegram

- TELEGRAM_BOT_TOKEN относится к digest bot.
- Бот должен быть администратором TELEGRAM_SOURCE_CHAT_ID.
- Для bot token не должен быть настроен webhook: getUpdates и webhook
  взаимоисключающие механизмы.
- Один bot token не должен одновременно обслуживаться другим getUpdates consumer.

## Месячный pipeline

Collector только накапливает данные. Подготовка выпуска остаётся отдельной:

~~~bash
guardamar-digest dedupe --period 2026-10 --semantic
guardamar-digest duplicate-review --period 2026-10
guardamar-digest classify --period 2026-10
guardamar-digest validate --period 2026-10
guardamar-digest preview --period 2026-10 --send
~~~

publish по-прежнему является отдельным явным действием.
