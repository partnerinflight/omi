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


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
