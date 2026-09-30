import unittest
from second_brain.adaptive import pipeline as m

CFG = {
    "vibe7_uncertainty_threshold": 72,
    "vibe7_importance_threshold": 86,
    "vibe7_important_uncertainty_threshold": 42,
    "vibe7_critical_signal_importance_threshold": 78,
    "vibe7_critical_signal_uncertainty_threshold": 30,
    "vibe7_rescue_uncertainty_threshold": 92,
    "memory_gate_enabled": True,
    "memory_gate_min_words": 10,
    "memory_gate_durability_threshold": 58,
    "memory_gate_retrieval_threshold": 55,
    "memory_gate_importance_override": 90,
    "memory_gate_importance_retrieval_floor": 48,
    "router_min_words": 12,
    "static_hotwords": ["DeepQuill", "Hermes", "Omi"],
    "hotword_max_terms": 32,
    "hotword_max_chars": 700,
}


def seg(text):
    return [{"start": 0.0, "end": 1.0, "speaker": "S1", "text": text}]


def test_family_chatter_is_filtered():
    text = "What are you doing? Do you want pancakes? Put your shoes on and come here please."
    gate = m.memory_gate_heuristic(text, seg(text), 24, [])
    keep, reason = m.evaluate_memory_gate(CFG, gate, 24, len(text.split()))
    assert gate["conversation_type"] == "family_chatter"
    assert keep is False


def test_family_conversation_with_event_is_kept():
    text = "Daniel's piano audition is October 3 at 4 PM and remember to schedule the lesson."
    signals = ["task", "numbers/dates"]
    gate = m.memory_gate_heuristic(text, seg(text), 78, signals)
    keep, reason = m.evaluate_memory_gate(CFG, gate, 78, len(text.split()))
    assert gate["contains"]["task"]
    assert gate["contains"]["date_or_event"]
    assert keep is True


def test_novelty_does_not_force_memory_keep():
    text = "What are you doing? Want pancakes? Come here and put your shoes on please."
    gate = m.memory_gate_heuristic(text, seg(text), 20, [])
    # There is deliberately no novelty argument to evaluate_memory_gate.
    keep, _ = m.evaluate_memory_gate(CFG, gate, 20, len(text.split()))
    assert keep is False


def test_low_value_uncertain_chatter_does_not_waste_7b():
    text = "What are you doing? Want pancakes? Put your shoes on and come here please."
    gate = m.memory_gate_heuristic(text, seg(text), 25, [])
    should = m.should_escalate_to_vibe7(
        CFG,
        25,
        80,
        {"importance_signals": []},
        gate,
    )
    assert should is False


def test_catastrophic_asr_can_still_rescue_with_7b():
    gate = {
        "conversation_type": "other_ephemeral",
        "durability": 5,
        "actionability": 5,
        "retrieval_value": 5,
        "contains": {},
        "durable_signal": False,
    }
    assert (
        m.should_escalate_to_vibe7(
            CFG,
            10,
            98,
            {"importance_signals": []},
            gate,
        )
        is True
    )


def test_durable_project_decision_kept():
    text = "We decided DeepQuill should launch at 39 dollars next Friday."
    signals = ["decision", "project", "numbers/dates"]
    gate = m.memory_gate_heuristic(text, seg(text), 92, signals)
    keep, _ = m.evaluate_memory_gate(CFG, gate, 92, len(text.split()))
    assert gate["conversation_type"] == "decision"
    assert keep is True


def test_windows_split_on_gap():
    segs = [
        {"start": 0.0, "end": 2.0, "speaker": "S1", "text": "hello"},
        {"start": 3.0, "end": 5.0, "speaker": "S2", "text": "hi"},
        {"start": 50.0, "end": 53.0, "speaker": "S1", "text": "new conversation"},
    ]
    windows = m.build_conversation_windows(segs, gap_seconds=35, max_seconds=180)
    assert len(windows) == 2


def test_uncertainty_detects_collapse():
    score, reasons = m.uncertainty_heuristic("!!!!!!!!!!!!!!!!!!!!!!!!!!!", [])
    assert score == 100


def test_hotwords_are_bounded_and_include_static():
    vault_context = [
        {"path": r"C:\Vault\Projects\DeepQuill Launch.md", "coverage": 0.6, "snippet": ""},
        {"path": r"C:\Vault\People\Daniel.md", "coverage": 0.4, "snippet": ""},
    ]
    catalog = ["DeepQuill Launch", "Daniel", "Random Project"]
    words = m.derive_hotwords(
        CFG,
        "Daniel and I were discussing the DeepQuill launch.",
        vault_context,
        catalog,
    )
    assert "DeepQuill" in words
    assert "Daniel" in words
    assert len(words) <= CFG["hotword_max_terms"]


def test_filler_like_and_a_lone_ill_are_not_durable():
    # Real chatter the v3 keywords kept: filler "like" and one "I'll" are not durable information.
    for text in ["Oh, that's how I like it. I just been enjoying it. I don't know what they are.",
                 "Uh I bet. I'll race it. Yeah, that's that's that's. Oh yeah. It can slide in."]:
        gate = m.memory_gate_heuristic(text, seg(text), 30, m.importance_heuristic(text, seg(text))[1])
        keep, _ = m.evaluate_memory_gate(CFG, gate, 30, len(text.split()))
        assert not gate["durable_signal"], text
        assert keep is False, text


def test_stated_preferences_and_backed_commitments_still_count():
    text = "Daniel likes sushi but he is allergic to shellfish, so keep that in mind."
    gate = m.memory_gate_heuristic(text, seg(text), 40, m.importance_heuristic(text, seg(text))[1])
    assert gate["contains"]["personal_durable_fact"]
    text = "I'll call the dentist tomorrow and book the cleaning for Daniel."
    gate = m.memory_gate_heuristic(text, seg(text), 60, m.importance_heuristic(text, seg(text))[1])
    assert gate["contains"]["commitment"] and gate["contains"]["task"]
    assert m.evaluate_memory_gate(CFG, gate, 60, len(text.split()))[0] is True


def test_importance_floor_is_off_by_default_and_drops_low_importance_windows():
    text = "We decided DeepQuill should launch at 39 dollars next Friday."
    gate = m.memory_gate_heuristic(text, seg(text), 25, ["decision", "project", "numbers/dates"])
    assert m.evaluate_memory_gate(CFG, gate, 25, len(text.split()))[0] is True
    keep, reason = m.evaluate_memory_gate(dict(CFG, memory_gate_min_importance=30), gate, 25, len(text.split()))
    assert keep is False and "floor" in reason


def test_hermes_is_consulted_only_for_borderline_keeps_and_can_veto_them():
    text = "I'll call the dentist tomorrow morning and book the cleaning for next week."
    gate = m.memory_gate_heuristic(text, seg(text), 35, ["commitment", "task"])
    words = len(text.split())
    borderline = dict(CFG, hermes_scoring_mode="borderline", hermes_borderline_max_importance=45)
    assert m.hermes_should_consult(CFG, gate, 35, words) is True, "default mode asks about every window"
    assert m.hermes_should_consult(borderline, gate, 35, words) is True
    assert m.hermes_should_consult(borderline, gate, 80, words) is False, "clearly important: no call"
    chatter = m.memory_gate_heuristic("Yeah. Oh yeah. So. Okay then.", seg("Yeah."), 10, [])
    assert m.hermes_should_consult(borderline, chatter, 10, 5) is False, "already dropped: no call"
    no = {"memory_keep": False, "memory_reason": "small talk"}
    assert m.apply_hermes_verdict(borderline, no, True, "kept") == (False, "Hermes: small talk")
    assert m.apply_hermes_verdict(borderline, None, True, "kept") == (True, "kept"), "Hermes down: keep"
    assert m.apply_hermes_verdict(CFG, no, True, "kept") == (True, "kept"), "v3 merge unchanged in default mode"


def test_hermes_key_can_come_from_a_private_file():
    import http.server, json, tempfile, threading
    from pathlib import Path
    seen = []

    class Hermes(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.headers.get("Authorization"))
            reply = {"importance": 20, "novelty": 10, "asr_uncertainty": 5, "memory_keep": False, "memory_reason": "chatter"}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hermes)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "hermes-key.txt"
            key.write_text("gateway-secret\n")
            cfg = dict(hermes_scoring_enabled=True, hermes_url=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                       hermes_api_key_file=str(key))
            result = m.hermes_score(cfg, "Yeah. Okay.", {}, [])
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["Bearer gateway-secret"]
    assert (result["memory_keep"], result["memory_reason"]) == (False, "chatter")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )


def load_tests(loader, tests, pattern):
    """These are plain test functions; wrap them so the unittest runner in scripts/test.py runs them."""
    suite = unittest.TestSuite()
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            suite.addTest(unittest.FunctionTestCase(test, description=name))
    return suite
