# Что сдаём (BCC-HUB × MNU FinTech Hackathon)

Срок: **27.11.2026, 23:59**. По условиям нужны демонстрация работы агента и репозиторий; отчёт и входной документ сдаются рядом со ссылкой на репозиторий.

| Что | Где |
| :--- | :--- |
| Репозиторий с агентом | https://github.com/FlyInTheSkyAndOfTheDarkness/for-bcc-finteck-hack (тег `v1.1`) |
| Проверяемый документ (выдан организаторами) | `docs/requirements.md`, та же версия в PDF: `docs/requirements.pdf` |
| Отчёт, сформированный агентом | `reports/report.md` (+ `report.html`, `report.json`); по PDF-входу — `reports/report_pdf.md` |
| Как повторить прогон | README → «Быстрый старт»: `python -m reqcheck docs/requirements.md -o reports/report.md` |
| Модель | Gemini `gemini-3.5-flash-lite`, `LLM_THINKING=high` (бесплатный ключ Google AI Studio) |
| Демо-видео | запись по сценарию `docs/DEMO_SCRIPT.md` |
| Оценка качества | `eval/results/final.md`, абляция `eval/results/ablation_*.md`, мутанты `eval/results/final_m*.md` |

Паспорт отчёта содержит название документа, SHA-256 файла, дату и время прогона, провайдера и модель, версию агента (`git describe`, тег `v1.1`) и версию промптов.
