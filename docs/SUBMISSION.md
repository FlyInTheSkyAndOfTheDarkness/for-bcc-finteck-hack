# Что сдаём (BCC-HUB × MNU FinTech Hackathon)

Срок: **27.11.2026, 23:59**. По условиям нужны демонстрация работы агента и репозиторий; отчёт и входной документ сдаются рядом со ссылкой на репозиторий.

| Что | Где |
| :--- | :--- |
| Репозиторий с агентом | https://github.com/FlyInTheSkyAndOfTheDarkness/for-bcc-finteck-hack (тег `v1.2.3`) |
| Проверяемый документ (выдан организаторами) | `docs/requirements.md`, та же версия в PDF: `docs/requirements.pdf` |
| Отчёт, сформированный агентом | `reports/report.md` (+ `report.html`, `report.json`) — прогон `v1.2` на основной модели; по PDF-входу — `reports/report_pdf.md` |
| Как повторить прогон | README → «Быстрый старт»: `python -m reqcheck docs/requirements.md -o reports/report.md` |
| Модель | Gemini `gemini-3.5-flash-lite`, `LLM_THINKING=high` (бесплатный ключ Google AI Studio) |
| Демо-видео | запись по сценарию `docs/DEMO_SCRIPT.md` |
| Оценка качества | `eval/results/final.md` и `final_judge.md` (судья-LLM), холодные прогоны `stats_cold2*.md` / `stats_cold3*.md`, абляция `eval/results/ablation_*.md`, мутанты `eval/results/final_m*.md`, контрольные прогоны `v1.2.2` — `v122_*.md`. Итог: в среднем 91% из 51 дефекта, 20/20 ключевых, 4/4 критичных (судья); лексически recall 78%, precision 80%, F1 79% |

Паспорт отчёта содержит название документа, SHA-256 файла, дату и время прогона, провайдера и модель, версию агента (`git describe`: у сдаваемого отчёта — тег `v1.2`) и версию промптов.
