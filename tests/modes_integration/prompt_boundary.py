"""Validate real Swift normalized text with the actual Python sender contract."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from supervisor.bridge import resolve_text
texts = json.load(sys.stdin)
assert [len(t) for t in texts] == [2000, 2000, 1999, 9], [len(t) for t in texts]
assert texts[1] == '👩🏽‍💻' * 500
assert texts[2] == 'x' * 1999
for text in texts:
    assert resolve_text({'automation_mode': 'rage', 'kind': 'followup', 'text': text}) == text
print('Swift/Python prompt boundaries: 4 passed')
