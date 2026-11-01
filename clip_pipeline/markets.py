"""Bounded authoritative market fetches and explicit development transports."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from .artifacts import ArtifactError, atomic_write, digest, json_bytes, read_json_model, _no_duplicates
from .contracts import MarketSnapshot

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MarketError(ArtifactError):
    pass


def fetch_markets(settings, cache_dir: Path, *, transport=None, now=None, sleep=time.sleep):
    import httpx
    from ssrf_guard import guarded
    now = now or datetime.now(timezone.utc)
    if settings.mode == 'snapshot':
        snapshot = read_json_model(Path(settings.path), MarketSnapshot)
        raw = Path(settings.path).read_bytes()
        return snapshot, dict(mode='snapshot', development=True, fetched_at=now.isoformat(),
                              version=snapshot.version, response_sha256=hashlib.sha256(raw).hexdigest(), etag=None)
    mock = settings.mode == 'mock_api'
    url = 'https://market-mock.invalid/markets' if mock else settings.url
    if mock:
        raw = Path(settings.path).read_bytes()
        etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
        transport = httpx.MockTransport(lambda request: httpx.Response(
            304 if request.headers.get('if-none-match') == etag else 200,
            headers={'etag': etag}, content=b'' if request.headers.get('if-none-match') == etag else raw))
    key = digest({'url': url, 'mock_path': settings.path if mock else None})
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / (key + '.json')
    cached = None
    if cache_path.exists():
        try:
            saved = json.loads(cache_path.read_text(), object_pairs_hook=_no_duplicates)
            cached_raw = bytes.fromhex(saved['raw_hex'])
            if hashlib.sha256(cached_raw).hexdigest() != saved['response_sha256']:
                raise ValueError()
            snapshot = MarketSnapshot.model_validate_json(cached_raw)
            age = (now - datetime.fromisoformat(saved['fetched_at'])).total_seconds()
            if age < 0:
                raise ValueError()
            cached = (saved, snapshot, age)
        except (ValueError, KeyError):
            raise MarketError('market_cache_invalid') from None
    headers = {'Accept': 'application/json'}
    if not mock and settings.bearer_token_env:
        token = os.environ.get(settings.bearer_token_env)
        if not token:
            raise MarketError('market_auth_missing')
        headers['Authorization'] = 'Bearer ' + token
    if cached and cached[0]['etag']:
        headers['If-None-Match'] = cached[0]['etag']
    with httpx.Client(transport=transport, timeout=15, follow_redirects=False, trust_env=False) as client:
        for attempt in range(3):
            try:
                with guarded(), client.stream('GET', url, headers=headers) as response:
                    if response.status_code == 304:
                        if not cached:
                            raise MarketError('unexpected_market_not_modified')
                        raw = bytes.fromhex(cached[0]['raw_hex'])
                        etag = response.headers.get('etag', cached[0]['etag'])
                    elif response.status_code == 429 or response.status_code >= 500:
                        raise httpx.TransportError('transient_market_failure')
                    elif response.status_code != 200:
                        raise MarketError('market_http_' + str(response.status_code))
                    else:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise MarketError('market_response_too_large')
                            chunks.append(chunk)
                        raw = b''.join(chunks)
                        etag = response.headers.get('etag')
                    if len(raw) > MAX_RESPONSE_BYTES or (etag and (len(etag) > 512 or '\n' in etag or '\r' in etag)):
                        raise MarketError('invalid_market_response')
                    json.loads(raw, object_pairs_hook=_no_duplicates)
                    snapshot = MarketSnapshot.model_validate_json(raw)
                    saved = dict(raw_hex=raw.hex(), response_sha256=hashlib.sha256(raw).hexdigest(),
                                 fetched_at=now.isoformat(), etag=etag)
                    atomic_write(cache_path, json_bytes(saved))
                    return snapshot, {k: v for k, v in saved.items() if k != 'raw_hex'} | dict(
                        mode=settings.mode, development=mock, version=snapshot.version, reused_cache=False)
            except httpx.TransportError:
                if attempt < 2:
                    sleep(2 ** attempt)
                    continue
                if cached and cached[2] <= (0 if mock else settings.max_cache_age_seconds):
                    return cached[1], {k: v for k, v in cached[0].items() if k != 'raw_hex'} | dict(
                        mode=settings.mode, development=mock, version=cached[1].version, reused_cache=True)
                raise MarketError('market_unavailable_or_stale') from None
            except MarketError:
                raise
            except (ValueError, UnicodeError):
                raise MarketError('invalid_market_response') from None
