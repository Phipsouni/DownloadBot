# DL-06 — Telegram-бот для скачивания видео и аудио

Бот принимает ссылки на отдельные ролики, отправляет MP4 или MP3 и позволяет владельцу вести список разрешённых пользователей. Он скачивает файлы до 75 МБ и при необходимости пытается сжать результат до 49 МБ перед отправкой. У обычного Telegram Bot API предел загрузки — 50 МБ; запас в 1 МБ помогает избежать ошибки 413. [Лимиты Telegram](https://core.telegram.org/bots/features)

## Что понадобится

- **Windows и Python 3.** Скачать с [python.org](https://www.python.org/downloads/). Эта версия бота проверялась с Python 3.14.
- **FFmpeg и ffprobe.** Возьмите сборку для Windows по ссылкам на [официальной странице FFmpeg](https://ffmpeg.org/download.html). Оба файла должны лежать рядом. Добавьте папку с ними в `PATH` либо укажите полный путь к `ffmpeg.exe` в `.env`.
- **JavaScript-среда для YouTube.** [yt-dlp рекомендует Deno](https://github.com/yt-dlp/yt-dlp/wiki/EJS): после установки из [официальной инструкции](https://docs.deno.com/runtime/getting_started/installation/) он используется автоматически. Если предпочитаете Node.js, установите его и укажите полный путь к `node.exe` в `.env`.
- **Токен Telegram-бота.** Создайте бота через [@BotFather по инструкции Telegram](https://core.telegram.org/bots/tutorial). Токен храните как пароль.

## Запуск

1. Выполните `install.bat` — он создаст `.venv` и установит Python-зависимости из `requirements.txt`.
2. Скопируйте `.env.example` в `.env` и заполните `TELEGRAM_BOT_TOKEN` и `TELEGRAM_ADMIN_ID` (числовой Telegram ID владельца). Если FFmpeg не находится через `PATH`, заполните `FFMPEG_PATH`.
3. Выполните `start.bat`.

Бот использует long polling. Не запускайте одновременно два экземпляра с одним токеном.

## Cookies

Для обычных публичных ссылок cookies не требуются. Если сервис запросит вход, создайте папку `site_cookies` и положите туда файл в формате Netscape/Mozilla для нужного сервиса:

| Сервис | Файл |
| --- | --- |
| YouTube и YouTube Music | `site_cookies/youtube.txt` |
| Instagram | `site_cookies/instagram.txt` |
| TikTok | `site_cookies/tiktok.txt` |

Бот читает файл только для сайта из ссылки и берёт из него cookies только соответствующего домена. Он **не читает cookies из браузера** и не обновляет эти файлы автоматически. Формат файлов и меры предосторожности описаны в [FAQ yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp). Не используйте экспорт всех cookies браузера: в таком файле могут оказаться действующие сеансы других сайтов.

## Что нельзя публиковать

`.env`, `site_cookies/`, `whitelist.txt`, `downloads/`, старые `cookies.txt` и `Token` содержат секреты или личные данные и исключены через `.gitignore`. Сборку FFmpeg, виртуальное окружение и локальные резервные копии тоже не нужно загружать в репозиторий. Перед публикацией проверьте список файлов в коммите: `.gitignore` не удаляет файлы, которые уже были добавлены в Git ранее.

Если токен когда-либо попал в публичный репозиторий, перевыпустите его через @BotFather. Если туда попали cookies, завершите соответствующие сеансы на сайтах и получите новые.
