# Что сдаём (BCC-HUB × MNU FinTech Hackathon)

Срок: **27.11.2026, 23:59**. По условиям нужны демонстрация работы агента и репозиторий; отчёт и входной документ сдаются рядом со ссылкой на репозиторий.

| Что | Где |
| :--- | :--- |
| Репозиторий с агентом | https://github.com/FlyInTheSkyAndOfTheDarkness/for-bcc-finteck-hack (тег `v1.3`) |
| Проверяемый документ (выдан организаторами) | `docs/requirements.md`, та же версия в PDF: `docs/requirements.pdf` |
| Отчёт, сформированный агентом | `reports/report.md` (+ `report.html`, `report.json`) — прогон `v1.2.4` на Claude Opus 5.5; для сравнения тот же агент на бесплатном Gemini — `reports/report_gemini.md`; по PDF-входу (Gemini) — `reports/report_pdf.md` |
| Как повторить прогон | README → «Быстрый старт»: `python -m reqcheck docs/requirements.md -o reports/report.md` |
| Модель | сдаваемый отчёт: Claude `claude-opus-5-5`, `LLM_EFFORT=medium` (`--provider anthropic`); по умолчанию бесплатный Gemini `gemini-3.5-flash-lite`, `LLM_THINKING=high` |
| Демо-видео | запись по сценарию `docs/DEMO_SCRIPT.md` |
| Оценка качества | `eval/results/final.md` и `final_judge.md` (судья-LLM), холодные прогоны `stats_cold2*.md` / `stats_cold3*.md`, абляция `eval/results/ablation_*.md`, мутанты `eval/results/final_m*.md`, контрольные прогоны `v1.2.2` — `v122_*.md`. Итог (Claude, сдаваемый отчёт): строго по цитатам recall 92%, 20/20 ключевых, 4/4 критичных, F1 83%; судья — 50/51. Gemini (среднее 3 прогонов): судья 91%, строго 78% |

Паспорт отчёта содержит название документа, SHA-256 файла, дату и время прогона, провайдера и модель, версию агента (`git describe`: у сдаваемого отчёта — тег `v1.2.4`) и версию промптов.
