"""Market API freshness, schema failures, conditional fetch and explicit mock mode."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import httpx
import pytest
from clip_pipeline.config import ApiMarkets, MockApiMarkets
from clip_pipeline.markets import fetch_markets, MarketError, MAX_RESPONSE_BYTES

PAYLOAD = {'schema_version':'1','version':'v1','markets':[{'market_id':'xyz:EXAMPLE',
           'underlying_name':'Example Corporation','asset_type':'equity','aliases':['Example'],'enabled':True}]}


def test_conditional_fetch_and_fresh_only_fallback(tmp_path):
    now=datetime.now(timezone.utc); settings=ApiMarkets(mode='api',url='https://example.invalid/markets',max_cache_age_seconds=60)
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json=PAYLOAD,headers={'etag':'"v1"'}) if len(seen)==1 else httpx.Response(304)
    transport=httpx.MockTransport(handler)
    first,meta=fetch_markets(settings,tmp_path,transport=transport,now=now)
    second,meta2=fetch_markets(settings,tmp_path,transport=transport,now=now+timedelta(seconds=10))
    assert second==first and seen[1].headers['if-none-match']=='"v1"'
    assert meta2['fetched_at']!=meta['fetched_at']
    down=httpx.MockTransport(lambda r:httpx.Response(503))
    _,fallback=fetch_markets(settings,tmp_path,transport=down,now=now+timedelta(seconds=20),sleep=lambda _:None)
    assert fallback['reused_cache']
    with pytest.raises(MarketError,match='stale'):
        fetch_markets(settings,tmp_path,transport=down,now=now+timedelta(seconds=100),sleep=lambda _:None)


@pytest.mark.parametrize('status',[401,403,302])
def test_auth_and_redirect_do_not_fallback(tmp_path,status):
    settings=ApiMarkets(mode='api',url='https://example.invalid/markets')
    fetch_markets(settings,tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=PAYLOAD)))
    with pytest.raises(MarketError,match='market_http'):
        fetch_markets(settings,tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(status)))


@pytest.mark.parametrize('kind',['duplicate','unknown_class','oversize','duplicate_json'])
def test_bad_market_response_fails_closed(tmp_path,kind):
    payload=json.loads(json.dumps(PAYLOAD))
    if kind=='duplicate':payload['markets']*=2
    if kind=='unknown_class':payload['markets'][0]['asset_type']='unknown'
    raw=json.dumps(payload).encode()
    if kind=='oversize':raw=b' '*(MAX_RESPONSE_BYTES+1)
    if kind=='duplicate_json':raw=b'{"version":"1","version":"2"}'
    with pytest.raises(MarketError):
        fetch_markets(ApiMarkets(mode='api',url='https://example.invalid/markets'),tmp_path,
                      transport=httpx.MockTransport(lambda r:httpx.Response(200,content=raw)))


def test_trade_xyz_mock_includes_all_index_markets(tmp_path):
    path=Path(__file__).resolve().parents[1]/'configs/development/tradexyz-markets.json'
    snapshot,meta=fetch_markets(MockApiMarkets(mode='mock_api',path=str(path)),tmp_path)
    provenance=json.loads(path.with_name('tradexyz-provenance.json').read_text())
    assert len(snapshot.markets)==provenance['market_count']==120
    by_id={m.market_id:m for m in snapshot.markets}
    assert len(by_id)==120
    assert {m.market_id for m in snapshot.markets if m.asset_type=='pre_ipo'}=={'xyz:ANT','xyz:OURA'}
    assert 'Anthropic' in by_id['xyz:ANT'].aliases and by_id['xyz:ANT'].enabled
    pre_ipo_source=provenance['sources'][1]
    assert pre_ipo_source['included_status']=='Live'
    assert set(pre_ipo_source['included_market_ids'])=={'xyz:ANT','xyz:OURA'}
    converted={'xyz:JIO','xyz:SHEIN','xyz:UNITREE','xyz:CXMT','xyz:SKHY','xyz:SPCX','xyz:QNT','xyz:CBRS'}
    assert set(pre_ipo_source['converted_market_ids_already_in_standard_index'])==converted
    assert all(by_id[market_id].asset_type=='equity' for market_id in converted)
    assert meta['development'] and meta['mode']=='mock_api'
    assert next(m for m in snapshot.markets if m.market_id=='xyz:SOXL').asset_type=='etf'
