"""Test-only loopback driver for the qualified public Codex worker entrypoint.

Accept only the test suite's private HOME, dummy credential and loopback URL.
This is not a runtime opt-in or a way to use real deployment credentials.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tomllib
from urllib.parse import urlsplit

from bridge.harness_worker import codex, emit


def main():
    home = Path(os.environ['HOME']).resolve()
    root = Path(__file__).resolve().parents[1] / '.bridge/native-codex-fixtures'
    if not home.is_relative_to(root) or os.environ.get('OPENAI_API_KEY') != 'offline-scripted-fixture':
        raise RuntimeError('fixture_only')
    config_home = Path(os.environ['CODEX_HOME']).resolve()
    if config_home != home / 'codex':
        raise RuntimeError('fixture_home_required')
    endpoint = urlsplit(tomllib.loads((config_home / 'config.toml').read_text())['openai_base_url'])
    if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or endpoint.username or endpoint.password:
        raise RuntimeError('fixture_loopback_required')
    config = json.loads(sys.stdin.readline())
    if Path(config['cwd']).resolve() != home / 'empty':
        raise RuntimeError('fixture_cwd_required')
    def observe(event):
        method = event.get('method', '')
        if method.startswith('item/'):
            # Test-only telemetry from the real native stream. No arguments,
            # file contents, credentials or worker-authored inference here.
            emit({'kind':'native_event', 'method':method,
                  'item_type':(event.get('params') or {}).get('item', {}).get('type')})

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(codex(config, observer=observe))
    except Exception as error:
        emit({'kind':'error','code':type(error).__name__})
    finally:
        loop.close()
        os._exit(0)


if __name__ == '__main__':
    main()
