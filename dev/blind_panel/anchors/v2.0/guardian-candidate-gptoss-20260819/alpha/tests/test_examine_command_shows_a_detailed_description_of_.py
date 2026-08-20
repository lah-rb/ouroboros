import pytest
from parser import parse_command, Command

def test_examine_maps_to_look():
    cmd = parse_command("examine sword")
    assert isinstance(cmd, Command), "parse_command should return a Command instance"
    assert cmd.verb == "look", "The verb for 'examine' should be mapped to 'look'"
    # The parser strips punctuation and lower‑cases tokens; the item name remains as a single token.
    assert cmd.args == ["sword"], "The argument list should contain the item identifier"

def test_inspect_maps_to_look():
    cmd = parse_command("inspect sword")
    assert isinstance(cmd, Command)
    assert cmd.verb == "look"
    assert cmd.args == ["sword"]

def test_describe_maps_to_look():
    cmd = parse_command("describe sword")
    assert isinstance(cmd, Command)
    assert cmd.verb == "look"
    assert cmd.args == ["sword"]
