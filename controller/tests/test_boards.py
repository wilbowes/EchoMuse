"""em_boards: the model Home Assistant shows for each board."""

import em_boards


def test_biscuit_keeps_the_model_every_existing_device_shows():
    # Changing it would show every existing HA device under a new model.
    assert em_boards.model("biscuit") == "Echo Dot Gen 2 (biscuit)"


def test_firmware_before_boards_and_unidentified_devices_read_as_biscuit():
    # Both run biscuit's layout, which is what the old string said.
    for board in (None, "", "unknown"):
        assert em_boards.model(board) == em_boards.MODELS["biscuit"]
        assert not em_boards.known(board)


def test_radar_is_its_own_model():
    assert em_boards.model("radar") == "Echo Gen 2 (radar)"


def test_a_board_newer_than_the_controller_is_not_called_biscuit():
    assert em_boards.model("rook") == "Echo (rook)"
    assert em_boards.known("rook")


def test_every_model_survives_project_name_split():
    # HA splits project_name on "." and shows the second part as the Model.
    for m in em_boards.MODELS.values():
        assert "." not in m


def test_every_board_the_firmware_knows_has_a_model():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "device/pkg/board/board.go").read_text()
    ids = set(re.findall(r'\bID:\s*"([a-z0-9_]+)"', src))
    assert ids, "no board ids found in board.go"
    assert ids <= set(em_boards.MODELS), ids - set(em_boards.MODELS)


def test_record_writes_only_when_a_board_changes():
    boards = {}
    stored = em_boards.record(boards, "035T", "radar")
    assert em_boards.decode(stored) == {"035T": "radar"}
    assert em_boards.record(boards, "035T", "radar") is None
    assert em_boards.record(boards, "VVV", "unknown") is None
    assert em_boards.record(boards, "VVV", None) is None
    assert boards == {"035T": "radar"}


def test_an_unreadable_store_reads_as_nothing_known():
    for raw in (None, "", "{", "[]", "3", '{"a": 1}'):
        assert em_boards.decode(raw) == {}
