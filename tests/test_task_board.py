"""Tests for the task board (tasks.json): checking the PM's plan, saving, resuming."""

import json

import pytest

import task_board

GOOD_PLAN = {
    "spec": "## Goal\nCount words.",
    "tasks": [
        {"title": "Count words", "description": "count_words(text) in wc.py", "examples": ["count_words('a b') -> 2"]},
        {"title": "CLI", "description": "python wc.py FILE", "examples": []},
    ],
}


def test_new_board_numbers_tasks_and_sets_todo():
    board = task_board.new_board("word counter", GOOD_PLAN)
    assert [t["id"] for t in board["tasks"]] == [1, 2]
    assert all(t["status"] == "todo" and t["attempts"] == 0 for t in board["tasks"])


@pytest.mark.parametrize("bad_plan, message", [
    ({"spec": "", "tasks": GOOD_PLAN["tasks"]}, "no spec"),
    ({"spec": "x", "tasks": []}, "no tasks"),
    ({"spec": "x", "tasks": 42}, "must be a JSON array .* sent a int"),
    ({"spec": "x", "tasks": "[{broken"}, "not valid JSON"),
    ({"spec": "x", "tasks": [{"title": "t", "description": "d"}] * 9}, "limit"),
    ({"spec": "x", "tasks": [{"title": "only a title"}]}, "title and a description"),
    ({"spec": "x", "tasks": [{"title": "t", "description": "d", "examples": "not a list"}]}, "must be a list"),
])
def test_bad_plans_are_rejected_with_a_reason(bad_plan, message):
    with pytest.raises(task_board.PlanError, match=message):
        task_board.new_board("idea", bad_plan)


def test_save_load_and_next_todo(tmp_path):
    path = tmp_path / "tasks.json"
    board = task_board.new_board("idea", GOOD_PLAN)
    board["tasks"][0]["status"] = "done"
    task_board.save(board, path)

    loaded = task_board.load(path)
    assert loaded == board
    assert task_board.next_todo(loaded)["id"] == 2


def test_task_prompt_contains_spec_task_and_examples():
    board = task_board.new_board("idea", GOOD_PLAN)
    prompt = task_board.task_prompt(board, board["tasks"][0])
    assert "Count words." in prompt                # the spec
    assert "YOUR TASK (1 of 2)" in prompt
    assert "count_words('a b') -> 2" in prompt     # the examples


def test_repeated_examples_are_dropped():
    plan = {"spec": "x", "tasks": [{"title": "t", "description": "d",
                                    "examples": ["f(1) -> 2", "f(1) -> 2", "f(0) -> 1"]}]}
    board = task_board.new_board("idea", plan)
    assert board["tasks"][0]["examples"] == ["f(1) -> 2", "f(0) -> 1"]


@pytest.mark.parametrize("example", ["roll(42) -> e.g. [3, 5]", "roll(0) -> depends on seed 0",
                                     "roll(1) -> something like [2]"])
def test_hedged_examples_are_rejected(example):
    plan = {"spec": "x", "tasks": [{"title": "t", "description": "d", "examples": [example]}]}
    with pytest.raises(task_board.PlanError, match="is a guess"):
        task_board.new_board("idea", plan)


def test_property_examples_are_fine():
    plan = {"spec": "x", "tasks": [{"title": "t", "description": "d",
                                    "examples": ["roll(5, 20, 0) -> 5 ints, each between 1 and 20"]}]}
    assert task_board.new_board("idea", plan)["tasks"][0]["examples"]


def test_tasks_sent_as_json_text_are_unpacked():
    # Seen live: the PM sent '[{"title": ...}]' as a string instead of a list.
    plan = {"spec": "x", "tasks": json.dumps(GOOD_PLAN["tasks"])}
    board = task_board.new_board("idea", plan)
    assert [t["title"] for t in board["tasks"]] == ["Count words", "CLI"]


def test_extra_closing_brackets_after_the_array_are_ignored():
    # Seen live: '[...]}' - the model also closed its outer object inside the string.
    plan = {"spec": "x", "tasks": json.dumps(GOOD_PLAN["tasks"]) + "}"}
    assert len(task_board.new_board("idea", plan)["tasks"]) == 2


def test_other_text_after_the_array_is_an_error():
    plan = {"spec": "x", "tasks": json.dumps(GOOD_PLAN["tasks"]) + " and more tasks later"}
    with pytest.raises(task_board.PlanError, match="not valid JSON"):
        task_board.new_board("idea", plan)
