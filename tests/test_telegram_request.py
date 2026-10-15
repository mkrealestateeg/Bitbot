import json
from urllib.parse import urlencode

def test_allowed_updates_is_one_json_parameter():
    value=json.dumps(["message","callback_query"])
    encoded=urlencode({"allowed_updates":value})
    assert encoded.count("allowed_updates=")==1
    assert "message" in encoded and "callback_query" in encoded

def test_expected_ui_callbacks():
    callbacks={"status","scan","auto","manual","safe","adventurer","coins","trades","closeall"}
    assert {"auto","manual","status","scan"}.issubset(callbacks)
