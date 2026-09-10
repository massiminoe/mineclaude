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


def test_collector_keeps_last_snapshot_only_for_run_threads(tmp_path):
    import os
    import subprocess
    art = tmp_path / 'artifacts'
    sessions = tmp_path / 'home' / 'sessions'
    art.mkdir()
    sessions.mkdir(parents=True)
    (art / 'codex-1.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': 'thread-a'}) + '\n')
    (art / 'codex-2.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': 'thread-a'}) + '\n')
    for identity in ['thread-a', 'unrelated']:
        events = [{'type': 'session_meta', 'payload': {'id': identity}}]
        events += [{'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
            'input_tokens': n, 'output_tokens': 5, 'cached_input_tokens': 50, 'private_extra': 'omit'
        }}}} for n in [100, 200]]
        (sessions / f'rollout-{identity}.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events) + '{partial')
    collector = Path(__file__).resolve().parents[1] / 'bench/harness/codex/collect_usage.mjs'
    subprocess.run(['node', str(collector)], env={**os.environ, 'CODEX_HOME': str(sessions.parent), 'ARTIFACTS_DIR': str(art)}, check=True)
    result = json.loads((art / 'codex-session-usage.json').read_text())
    assert result['sessions'] == [{'thread_id': 'thread-a', 'usage': {'input_tokens': 200, 'output_tokens': 5, 'cached_input_tokens': 50}}]
