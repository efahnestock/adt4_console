"""Mock objects get object symbols O(900001), ... (mission.mocks)."""

import pytest

from adt4_console.mission import mocks
from adt4_console.mission.model import Mission


def test_symbols_fill_in_order_and_keep_their_own():
    p = [{"label": "a", "pos": [0, 0]}, {"label": "b", "pos": [1, 1], "sym": "O(900001)"}, {"label": "c", "pos": [2, 2]}]
    mocks.fill_symbols(p)
    assert [x["sym"] for x in p] == ["O(900002)", "O(900001)", "O(900003)"]
    assert mocks.is_mock("o900001") and mocks.is_mock("O(900001)") and not mocks.is_mock("O(12)")
    assert mocks.position(p, "o900003") == [2, 2] and mocks.position(p, "O(12)") is None


def test_missions_get_symbols_and_goals_name_them():
    m = Mission.model_validate({"text": "t", "planted": [{"label": "crate", "pos": [3, 4]}]})
    assert m.planted[0].sym == "O(900001)"
    assert mocks.named("(and (inspected-by spot1 o900001) (visited-object o12))", m.planted) == {
        900001: ("crate", [3, 4])
    }
    with pytest.raises(ValueError, match="o900009"):
        mocks.named("(visited-object o900009)", m.planted)
