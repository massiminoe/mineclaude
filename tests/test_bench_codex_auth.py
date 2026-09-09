"""Subscription-only auth and secure staging contract."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('codex_auth', Path(__file__).resolve().parents[1] / 'bench/codex_auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


def test_accepts_subscription(tmp_path):
    path = tmp_path / 'auth.json'
    data = {'auth_mode': 'chatgpt', 'tokens': {k: 'test' for k in ('access_token', 'refresh_token', 'id_token')}}
    path.write_text(json.dumps(data))
    assert auth.subscription_auth(path) == data


@pytest.mark.parametrize('data', [
    [],
    {'auth_mode': 'chatgpt', 'tokens': 'invalid'},
    {'auth_mode': 'apikey', 'OPENAI_API_KEY': 'test'},
    {'auth_mode': 'chatgpt', 'tokens': {}},
    {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': 'test', 'tokens': {k: 'test' for k in ('access_token', 'refresh_token', 'id_token')}},
])
def test_rejects_missing_tokens_or_api_fallback(tmp_path, data):
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        auth.subscription_auth(path)
