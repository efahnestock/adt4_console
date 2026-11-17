"""Mock objects get the next free object symbols after the map's (mission.mocks)."""

from adt4_console.mission import mocks


def test_symbols_number_on_from_the_map_and_keep_their_own():
    p = [
        {"label": "a", "pos": [0, 0]},
        {"label": "b", "pos": [1, 1], "sym": "O(701)"},
        {"label": "c", "pos": [2, 2]},
    ]
    mocks.fill_symbols(p, 700)
    assert [x["sym"] for x in p] == ["O(702)", "O(701)", "O(703)"]
    assert mocks.max_real(["O(699)", "O(12)", "P(5)"]) == 699
    assert (
        mocks.is_mock("o701", p)
        and mocks.is_mock("O(703)", p)
        and not mocks.is_mock("O(12)", p)
    )
    assert mocks.position(p, "o703") == [2, 2] and mocks.position(p, "O(12)") is None


def test_goals_name_them_and_a_taken_symbol_is_renumbered():
    p = mocks.fill_symbols(
        [{"label": "crate", "pos": [3, 4]}, {"label": "bag", "pos": [5, 6]}], 700
    )
    goal = "(and (inspected-by spot1 o700) (visited-object o701) (visited-object o12))"
    assert mocks.named(goal, p) == {700: ("crate", [3, 4]), 701: ("bag", [5, 6])}
    moved = mocks.renumber(p, {700}, 701)  # a real O(700) arrived; O(701) is the bag's
    assert moved == {"o700": "o702"} and p[0]["sym"] == "O(702)"
    assert (
        mocks.rewrite(goal, moved)
        == "(and (inspected-by spot1 o702) (visited-object o701) (visited-object o12))"
    )
