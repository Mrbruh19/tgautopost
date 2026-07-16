# Telegram Car Publisher v1

Сервис принимает текст и 4 прямые HTTPS-ссылки на фотографии, скачивает изображения и публикует их одним альбомом в Telegram-канал.

## Переменные среды Railway

- `TELEGRAM_BOT_TOKEN` — новый токен бота.
- `TELEGRAM_CHAT_ID` — username канала, например `@my_auto_channel`, либо цифровой ID.
- `PUBLISH_API_KEY` — длинный секретный ключ, придуманный вами.

Не публикуйте токен и API-ключ.

## Развёртывание

1. Распакуйте архив.
2. Создайте репозиторий GitHub и загрузите файлы.
3. В Railway: **New Project → Deploy from GitHub repo**.
4. Добавьте три переменные среды.
5. Создайте публичный домен в разделе Networking.
6. Проверьте `https://ВАШ-ДОМЕН/health`.

Ожидаемый ответ:

```json
{"status":"ok"}
```

## Запрос публикации

`POST https://ВАШ-ДОМЕН/publish`

Заголовок:

`X-API-Key: ВАШ_PUBLISH_API_KEY`

JSON:

```json
{
  "caption": "🚘 <b>Тестовый автомобиль</b>\n\n💰 <b>Цена: 1 500 000 ₽</b>",
  "photo_urls": [
    "https://example.com/photo1.jpg",
    "https://example.com/photo2.jpg",
    "https://example.com/photo3.jpg",
    "https://example.com/photo4.jpg"
  ]
}
```

Следующий этап после успешного теста — автоматическое удаление надписи «明起车业 / АВТОМИР».
