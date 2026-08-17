# ATP — TikTok архиватор

Коротко: ATP импортирует ваши лайки/сохранённые видео из TikTok, скачивает новые видео и публикует их в чат Discord.

# ВАЖНО!!! - Я навайбкодил эту хуйню через нейрохуйню какуюто, я удивлен что оно хоть как то работает, если у вас будут какието ошибки, попробуйте как то исправить, мб почините если вы умный но не в моем случае, у меня по крайней мере работает, кроме слайдшоу, хуево склеивает, но я потом когда нибуть попробую починить. Если вам не лень, можете доработать эту залупу, по ощущениям у меня не хватит силы воли продолжить тут ковырятся.

---

## Быстрый старт (локально, Windows / PowerShell)

1. Создайте и активируйте виртуальное окружение (в каталоге проекта):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

2. Установите зависимости:

```powershell
python -m pip install -r requirements.txt
```

3. Установите ffmpeg (нужен для обработки слайдшоу):

- Scoop (без прав администратора):
```powershell
iwr -useb get.scoop.sh | iex
scoop install ffmpeg
```
- Или скачать статический билд и добавить `bin` в PATH.

4. Запустите ATP:

```powershell
python -m atp
```

---

## Основные файлы и поведение

- `config/settings.conf` — главный конфиг (при первом запуске создаётся из `example.settings.conf`).
- `downloads/` — куда сохраняются медиа (можно изменить через `DOWNLOADS_DIR`).
- База: `config/tiktok_videos.db` — SQLite.

Поведение парсера:
- По умолчанию `PROCESS_BACKLOG_ON_STARTUP=false` — при старте мы НЕ обрабатываем весь бэклог старых записей (чтобы не затопить сеть скачиваниями). Оставьте это значение, если не уверены.
- Программа делает частый опрос сохранённых видео (по умолчанию каждые 15 секунд) и импортирует только новые видео, которые появились после запуска процесса (runtime-baseline). Это предотвращает массовую загрузку старых сохранённых видео.

---

## Обязательные настройки (в `config/settings.conf`)

- `TIKTOK_USER` — ваш ник (без `@`) если хотите автопарсинг. Без него автопарсинг отключён.
- `DOWNLOAD_SAVED_VIDEOS` / `DOWNLOAD_LIKED_VIDEOS` — включают соответствующие импорты.
- `COOKIES_FILE` — путь к cookies (Netscape) для импорта сохранённых видео (необходим для приватных настроек).
- `DISABLE_IMPERSONATION` — если установлено в `true`, yt-dlp не будет пытаться использовать impersonation; полезно когда impersonation extras не установлены.
- `USER_AGENT` — при проблемах с сетевыми ошибками задайте современный UA, например Chrome UA.

Рекомендация: заполните только то, что необходимо; используйте переменные окружения CI/хоста для секретов.

---

## Рабочие сценарии и полезные команды

- Импорт и проверка вручную (однопроходный):
```powershell
python -c "from atp.video_import import import_from_tiktok; import_from_tiktok()"
```
- Быстрая проверка доступности TikTok из окружения (15s timeout):
```powershell
python -c "import requests; print(requests.get('https://www.tiktok.com', timeout=15).status_code)"
```
- Включить временный обход impersonation (если вы не ставите extras):
```powershell
$env:DISABLE_IMPERSONATION = 'true'
python -m atp
```

---

## Поведение при проблемах с TikTok

- Логи могут показывать: "Solving JS challenge" — это обычный анти-бот этап. Если видите постоянные ошибки, попробуйте:
  - задать `USER_AGENT` в `config/settings.conf`,
  - включить `DISABLE_IMPERSONATION=true`,
  - или установить impersonation extras (Playwright + yt-dlp[impersonate]).

---