"""Plain-text admin reports. All displayed timestamps use Moscow time."""
from datetime import datetime

from cloudflare_analytics import MOSCOW


def volume(size):
    if size >= 1_000_000_000:
        return f'{size / 1_000_000_000:.2f} ГБ'
    if size >= 1_000_000:
        return f'{size / 1_000_000:.2f} МБ'
    if size >= 1000:
        return f'{size / 1000:.2f} КБ'
    return f'{size} Б'


def stamp(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime('%d.%m.%Y %H:%M')


def storage_report(snapshot=None, error=None, operations=None, analytics_error=None):
    lines = ['☁️ Хранилище R2']
    if error is not None:
        lines += ['🔴 Не активен', f'Причина: {error}',
                  'Занято / файлы / активные ссылки / очистки: нет данных.']
    else:
        lines += ['🟢 Активен',
                  f"Занято: {volume(snapshot['bytes'])} / 10 ГБ",
                  f"Рабочий лимит бота: {volume(snapshot['limit'])}",
                  f"Файлов: {snapshot['objects']}",
                  f"Активных ссылок: {snapshot['active_links']}", '', 'Ближайшие очистки (МСК):']
        if snapshot['expired']:
            lines.append(f"• Просрочено: {snapshot['expired']}; повтор удаления при проверке, каждые 5 мин.")
        for cleanup in snapshot['upcoming']:
            lines.append(f"• {stamp(cleanup['at'])} — {cleanup['count']} файл., {volume(cleanup['bytes'])}")
        if not snapshot['expired'] and not snapshot['upcoming']:
            lines.append('• Нет файлов, ожидающих очистки.')
        if snapshot['untracked']:
            lines.append(f"Без записи срока в базе бота: {snapshot['untracked']} файл.")
        lines.append('Время очистки — срок файла; проверка каждые 5 минут, пока бот запущен.')
    if operations is not None:
        lines += ['', f"Операции за {operations['month']} (календарный месяц, МСК): {operations['total']:,}",
                  f"• Класс A: {operations['a']:,} / 1 000 000 бесплатно в месяц",
                  f"• Класс B: {operations['b']:,} / 10 000 000 бесплатно в месяц",
                  f"• Бесплатные: {operations['free']:,}"]
        if operations['other']:
            lines.append(f"• Другие: {operations['other']:,}")
        lines.append(f"Запрос аналитики: {stamp(operations['as_of'])} МСК. Данные Cloudflare могут запаздывать.")
        lines.append('Показаны операции этого хранилища; бесплатные квоты Standard общие для аккаунта. '
                     'Это календарный месяц МСК, не расчётный период Cloudflare.')
    else:
        lines += ['', f'Операции за месяц: нет данных. {analytics_error or "Аналитика недоступна."}']
    return '\n'.join(lines)


def traffic_report(totals):
    return ('📊 Статистика трафика\n'
            f"Начало учёта: {stamp(totals['started_at'])} МСК\n\n"
            f"Скачано из источников на ПК: {volume(totals['source_bytes'])}\n"
            f"Успешно отправлено в Telegram: {volume(totals['telegram_bytes'])}\n"
            f"Успешно загружено в R2: {volume(totals['r2_bytes'])}\n\n"
            'Скачивания включают полученные медиаданные частичных и повторных попыток. '
            'Передача в Telegram учитывается после сжатия; в R2 — размер готового файла или ZIP. '
            'Скачивания пользователями из R2 не учитываются. История до начала учёта отсутствует.')
