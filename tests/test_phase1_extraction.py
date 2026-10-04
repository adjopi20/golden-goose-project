import ast
from datetime import date
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_extracted_functions_preserve_original_asts():
    checks=json.loads((ROOT/'tests/fixtures/phase1_extracted_function_hashes.json').read_text())
    for check in checks:
        tree=ast.parse((ROOT/check['file']).read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==check['function'])
        assert hashlib.sha256(ast.dump(node).encode()).hexdigest()==check['sha256'],check


def test_shared_profile_matches_original_geometry_exactly():
    from models.trend_following_preny_profile_15m.runtime.preparation import make_profile
    for case in json.loads((ROOT/'tests/fixtures/preny_profile_geometry_v1.json').read_text()):
        actual=make_profile(date(2026,10,3),dict(case['prices']))
        assert actual==case['expected']
