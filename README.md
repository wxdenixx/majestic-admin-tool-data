# majestic-admin-tool-data

Публичный data-репозиторий для [Majestic Admin Tool](https://github.com/wxdenixx/majestic-admin-tool)
(приватный репо с исходниками). Здесь лежат **актуальные скраппированные данные**:
правила сервера Majestic RP из форума и каталог предметов из wiki.

## Что тут есть

```
.
├── manifest.json             ← главный индекс: версии, SHA-256, ссылки на пакеты
├── rules/                    ← JSON-пакеты с правилами, по одному на раздел форума
│   ├── general.json          ← основные правила проекта
│   ├── military.json         ← военное положение
│   ├── families.json         ← семейные организации
│   └── ...                   ← итого 16 разделов
├── items/
│   └── all.json              ← каталог предметов из wiki.majestic-rp.ru
├── scripts/                  ← Python-скрейперы
├── schema/                   ← JSON Schema 2020-12 для валидации
└── .github/workflows/        ← cron, который обновляет данные раз в сутки
```

## Для пользователей тулса

Ничего делать не нужно — сам тулс (начиная с 0.4.0) умеет подтягивать отсюда
свежие правила через встроенный `IDataSyncService`. Достаточно открыть
**Settings → Data Sync → Синхронизировать сейчас** или включить
`autoSyncOnStartup: true`.

URL для клиента по умолчанию:

```
https://raw.githubusercontent.com/wxdenixx/majestic-admin-tool-data/main/manifest.json
```

## Для разработчиков

Подробный контракт формата — в основном репо:
[`docs/DATA_SYNC.md`](https://github.com/wxdenixx/majestic-admin-tool/blob/main/docs/DATA_SYNC.md).

### Запустить скрейперы локально

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r scripts/requirements.txt
playwright install chromium

# скрейпим правила (требуется Chromium для обхода JS-challenge)
python scripts/scrape_rules.py

# скрейпим wiki-items (чистый requests)
python scripts/scrape_items.py

# пересобираем manifest.json
python scripts/build_manifest.py
```

### Автоматический прогон

GitHub Actions запускает `.github/workflows/scrape.yml` по расписанию
`0 6 * * *` UTC (9:00 MSK). Можно запустить вручную через вкладку
**Actions → Scrape forum rules + wiki items → Run workflow**.

## Лицензия

Скрипты (`scripts/`, `schema/`, `.github/`) — **MIT**.

Данные (`rules/`, `items/`, `manifest.json`) — принадлежат Majestic RP и
используются в образовательных/служебных целях администраторами проекта.
При коммерческом использовании — уточняйте у правообладателей.

## Безопасность

- Репозиторий только публикует GET-данные. Никогда не принимает/хранит
  персональные данные пользователей.
- Скрейперы работают только с публичными страницами форума и wiki.
- User-Agent честно идентифицируется как `majestic-admin-tool-scraper`.
