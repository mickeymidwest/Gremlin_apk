"""Reflexion: a lost battle leaves a one-line lesson, loaded next time
for a similar task."""
from gremlin_core.magic import reflexion
from gremlin_core.magic.model import ScriptedModel
from gremlin_core.magic.types import Task, Transcript, StepRecord


def _lost_transcript():
    return Transcript(task_id="t", final_message="(gave up: stuck repeating one action)",
                      steps=[StepRecord(kind="model", content="let me read the file again"),
                             StepRecord(kind="tool", tool_name="read_file", content="ok",
                                        tool_result="package x")])


def test_distil_save_and_load_by_keyword_overlap(tmp_path):
    root = str(tmp_path)
    task = Task(id="klon", tags=["android", "kotlin"],
               prompt="implement the klondike solitaire deal() method in Game.kt")
    model = ScriptedModel(["Write the implementation after reading once, don't re-read."])

    lesson = reflexion.distil_lesson(model, task, _lost_transcript())
    assert "read" in lesson.lower()
    reflexion.save_lesson(root, task, lesson)

    similar = Task(id="k2", tags=["kotlin"], prompt="finish the klondike Game.kt implementation")
    assert reflexion.load_lessons(root, similar) == [lesson]

    unrelated = Task(id="u", prompt="fix a python import error in the flask server")
    assert reflexion.load_lessons(root, unrelated) == []


def test_no_lessons_file_is_fine(tmp_path):
    assert reflexion.load_lessons(str(tmp_path), Task(id="x", prompt="anything")) == []


def test_empty_lesson_not_saved(tmp_path):
    reflexion.save_lesson(str(tmp_path), Task(id="x", prompt="thing about kotlin gradle"), "")
    assert not (tmp_path / "data" / "magic" / "lessons.jsonl").exists()


def test_distil_returns_empty_on_a_junk_reply(tmp_path):
    model = ScriptedModel([""])   # model gave nothing
    assert reflexion.distil_lesson(model, Task(id="t", prompt="x"), _lost_transcript()) == ""
