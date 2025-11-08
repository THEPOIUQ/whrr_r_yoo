# whrr_r_yoo

Скрипт для парсинга поиска на [yellowpages.com](https://www.yellowpages.com/) с использованием Playwright.

## Запуск

```bash
python yellowpages_scraper.py "chicken" "Los Angeles, CA" --pages 2
```

Скрипт автоматически:

1. Разогревает сайт в безголовом Chromium и собирает актуальные cookies.
2. Выполняет HTTP-запросы через `requests`, переиспользуя заголовки и cookies из браузера.
3. При ответе `403 Forbidden` повторяет попытку через Playwright, чтобы скрипт не падал.
4. Парсит карточки компаний и сообщает, сколько записей получено.

Для работы требуется установленный Playwright и браузеры (`playwright install`).
