"""투표 집계를 Firestore에서 모아 GitHub에 올릴 스냅샷으로 만든다.

앱이 Firestore를 직접 읽으면 사용자 수만큼 읽기가 늘어난다.
그래서 수집기가 주기적으로 한 번만 읽어 파일로 만들고, 앱은 그 파일을 받는다.
→ Firestore 읽기가 사용자 수와 무관해진다.

**바뀐 문서만 읽는다.** 각 문서의 updatedAt을 보고 지난 실행 이후 갱신된 것만
가져와 이전 스냅샷에 덮어쓴다. 그래서 문서가 몇 개든 실행당 읽기가 적다.
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

PROJECT = 'highway-rest-70324'
BASE = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
SA = os.environ.get('FIREBASE_SA', '')   # 서비스 계정 키(JSON 문자열)
OUT = os.environ.get('SNAPSHOT_PATH', 'votes.json')

_token = None


def token():
    """서비스 계정 액세스 토큰.

    App Check를 켜면 API 키 호출은 막힌다. 서비스 계정은 관리자 자격이라
    App Check 검증 대상이 아니므로 수집기는 이쪽으로 붙는다.
    """
    global _token
    if _token is None:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        creds = service_account.Credentials.from_service_account_info(
            json.loads(SA),
            scopes=['https://www.googleapis.com/auth/datastore'])
        creds.refresh(Request())
        _token = creds.token
    return _token


def _val(v):
    """Firestore 값 표현을 파이썬 값으로."""
    if 'integerValue' in v: return int(v['integerValue'])
    if 'doubleValue' in v: return float(v['doubleValue'])
    if 'timestampValue' in v: return v['timestampValue']
    if 'stringValue' in v: return v['stringValue']
    return None


def run_query(collection, since):
    """updatedAt이 since 이후인 문서만 가져온다. since가 없으면 전체."""
    q = {'structuredQuery': {'from': [{'collectionId': collection}]}}
    if since:
        q['structuredQuery']['where'] = {
            'fieldFilter': {
                'field': {'fieldPath': 'updatedAt'},
                'op': 'GREATER_THAN',
                'value': {'timestampValue': since},
            }
        }
        q['structuredQuery']['orderBy'] = [
            {'field': {'fieldPath': 'updatedAt'}, 'direction': 'ASCENDING'}]
    req = urllib.request.Request(
        f'{BASE}:runQuery',
        data=json.dumps(q).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {token()}'})
    with urllib.request.urlopen(req, timeout=40) as r:
        rows = json.loads(r.read().decode())
    out = {}
    for row in rows:
        doc = row.get('document')
        if not doc:
            continue
        did = doc['name'].split('/')[-1]
        out[did] = {k: _val(v) for k, v in doc.get('fields', {}).items()
                    if _val(v) is not None}
    return out


HIST = 'history'


def _monthly(rest):
    """오늘 값을 하루 한 번 보관하고, 30일 전 값과의 차이를 돌려준다.

    메뉴 순위는 휴게소 문서 안의 m<seq>_like 를 훑어 만든다.
    (예전에는 menuTop 컬렉션을 따로 뒀지만, 집계를 파일로 받으면서 불필요해졌다.)
    """
    os.makedirs(HIST, exist_ok=True)
    today = datetime.now(timezone.utc).date()

    # 오늘 치가 없으면 남긴다 (하루 1개)
    todays = f'{HIST}/{today.isoformat()}.json'
    if not os.path.exists(todays):
        flat = {}
        for rid, fields in rest.items():
            for k, v in fields.items():
                if k.startswith('m') and k.endswith('_like'):
                    flat[f'{rid}|{k}'] = v
        json.dump(flat, open(todays, 'w'), sort_keys=True)

    # 30일 넘은 파일은 지운다
    files = sorted(f for f in os.listdir(HIST) if f.endswith('.json'))
    for f in files:
        try:
            d = datetime.fromisoformat(f[:-5]).date()
        except ValueError:
            continue
        if (today - d).days > 40:
            os.remove(f'{HIST}/{f}')

    # 30일 전(없으면 가장 오래된) 파일을 기준으로 삼는다
    files = sorted(f for f in os.listdir(HIST) if f.endswith('.json'))
    target = (today - timedelta(days=30)).isoformat() + '.json'
    base_name = None
    for f in files:
        if f <= target:
            base_name = f
    if base_name is None and files:
        base_name = files[0]
    if base_name is None:
        return {}

    try:
        base = json.load(open(f'{HIST}/{base_name}'))
    except Exception:
        return {}

    # 결과는 rest 와 같은 모양으로 돌려준다 (앱이 동일한 코드로 처리한다)
    out = {}
    for rid, fields in rest.items():
        for k, v in fields.items():
            if not (k.startswith('m') and k.endswith('_like')):
                continue
            gain = v - base.get(f'{rid}|{k}', 0)
            if gain > 0:
                out.setdefault(rid, {})[k] = gain
    return out


def main():
    if not SA:
        print('FIREBASE_SA 가 없습니다', file=sys.stderr)
        sys.exit(1)

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}

    since = prev.get('at')
    # 첫 실행이거나 오래됐으면 전체를 다시 받는다
    full = since is None
    if since:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                since.replace('Z', '+00:00'))
            if age > timedelta(days=1):
                full = True
        except Exception:
            full = True

    rest = dict(prev.get('rest') or {})

    new_rest = run_query('rest', None if full else since)

    if full:
        rest = {}
    rest.update(new_rest)

    # 화면에 쓰지 않는 값은 빼서 파일을 가볍게 유지한다
    def clean(d):
        return {k: {kk: vv for kk, vv in v.items()
                    if kk != 'updatedAt' and isinstance(vv, int) and vv != 0}
                for k, v in d.items()}

    rest = {k: v for k, v in clean(rest).items() if v}

    # ── 최근 한 달 인기 ────────────────────────────────
    # Firestore는 누적값만 갖고 있어 '요즘 인기'를 알 수 없다.
    # 그래서 하루에 한 번 스냅샷을 남겨두고, 30일 전 값과의 차이를 낸다.
    month = _monthly(rest)

    data = {
        'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'mode': 'full' if full else 'incremental',
        'rest': rest,
        'menuMonth': month,
    }
    json.dump(data, open(OUT, 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{'전체' if full else '변경분'} 수집 — "
          f"읽은 문서 {len(new_rest)}개 / 누적 휴게소 {len(rest)}곳")


if __name__ == '__main__':
    main()
