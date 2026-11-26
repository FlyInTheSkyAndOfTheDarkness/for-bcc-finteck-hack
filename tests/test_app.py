from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_app_runs_sample_with_fake_provider():
    at = AppTest.from_file(APP, default_timeout=180).run()
    assert not at.exception
    at.selectbox(key="provider").select("fake (демо без ключа)")
    at.run()
    at.button(key="run_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == ["Итог", "Нарезка", "Замечания", "Уточняющие вопросы",
                                          "Отчёт", "Трассировка"]
    assert at.session_state["data"].verdict is not None
    assert len(at.metric) >= 4


def test_app_recalculate_with_answers():
    from reqcheck.models import Question
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.selectbox(key="provider").select("fake (демо без ключа)")
    at.run()
    at.button(key="run_btn").click().run()
    data = at.session_state["data"]
    data.questions = [Question(qid="Q-01", question="Какой лимит?", why="нужно для проверки",
                               options=["100", "200"])]
    at.session_state["data"] = data
    at.run()
    at.radio(key="ans_Q-01").set_value("100")
    at.button(key="recalc_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["data"] is not None
