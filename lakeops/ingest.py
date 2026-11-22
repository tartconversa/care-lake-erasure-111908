"""后续资料接收：墓碑拦截、依据窗口校验、派生联动更新。

撤回后补到的离线包不能让已清除内容重新出现：
- 整人清除后，无清除后重新建立的有效依据一律拒收；
- 已清除的服务键不得复用；
- 已清除的字段值永远剔除，字段级撤回在依据重新建立前持续生效；
- 重新建立的授权只放行其有效期窗口内的新服务数据。
接收日志只记录键控散列与字段名，不记录个人值。
"""
from __future__ import annotations

import json

from . import util
from .core import SERVICE_FIELDS, compute_person_day, find_detail_copy, get_copies


def normalize_payloads(obj):
    """接受单条载荷、载荷列表、投递信封或信封列表，统一为载荷列表。"""
    if isinstance(obj, dict) and 'payload' in obj:
        obj = [obj]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and 'payload' in obj[0]:
        payloads = []
        for envelope in obj:
            payload = envelope['payload']
            payloads.extend(payload if isinstance(payload, list) else [payload])
        return payloads
    if isinstance(obj, dict):
        return [obj]
    return list(obj)


def _find_authority(store, person_id, partner, date):
    for auth in store.q(
        "SELECT * FROM authorities WHERE kind='service' AND person_id=? AND partner=?",
        (person_id, partner),
    ):
        if auth['from_date'] <= date and (auth['until_date'] is None or date < auth['until_date']):
            return auth
    return None


def _granted_after(auth, tombstone):
    return util.parse_time(auth['granted_at']) > util.parse_time(tombstone['purged_at'])


def _field_lifted(store, person_id, partner, date, field, tombstone):
    """字段撤回是否被清除后重新建立的依据解除（仅对该字段、该窗口）。"""
    for auth in store.q(
        "SELECT * FROM authorities WHERE kind='service' AND person_id=? AND partner=?",
        (person_id, partner),
    ):
        if not (auth['from_date'] <= date and (auth['until_date'] is None or date < auth['until_date'])):
            continue
        if field not in json.loads(auth['fields']):
            continue
        if _granted_after(auth, tombstone):
            return True
    return False


def _ingest_one(store, secret, payload, existing_ids):
    missing = [k for k in SERVICE_FIELDS if payload.get(k) is None]
    if missing:
        return 'rejected', '载荷字段缺失: ' + ','.join(missing), None, []
    pid, partner, day, sid = (payload[k] for k in ('person_id', 'partner', 'date', 'service_id'))
    person_token = util.keyed(secret, 'person', pid)
    full_tomb = store.one(
        "SELECT * FROM tombstones WHERE kind='person' AND person_token=?", (person_token,)
    )
    auth = _find_authority(store, pid, partner, day)
    if full_tomb is not None and (auth is None or not _granted_after(auth, full_tomb)):
        return 'rejected', '历史身份已清除，且无清除后重新建立的有效依据', None, []
    if store.one('SELECT 1 AS x FROM tombstones WHERE value_hash=?', (util.keyed(secret, 'value', sid),)):
        return 'rejected', '服务键已清除，不得复用', None, []
    if sid in existing_ids:
        return 'skipped', '重复投递，幂等跳过', None, []
    if auth is None:
        return 'rejected', '无有效处理依据', None, []

    stripped = []
    row = dict(payload)
    for key, value in list(row.items()):
        if key in SERVICE_FIELDS or not isinstance(value, str):
            continue
        if store.one('SELECT 1 AS x FROM tombstones WHERE value_hash=?', (util.keyed(secret, 'value', value),)):
            row[key] = None
            stripped.append(key + '(撤回值)')
    for tomb in store.q(
        "SELECT * FROM tombstones WHERE kind='field' AND person_token=? AND (partner IS NULL OR partner=?)",
        (person_token, partner),
    ):
        field = tomb['field']
        if row.get(field) is None:
            continue
        if _field_lifted(store, pid, partner, day, field, tomb):
            continue
        row[field] = None
        if not any(s.startswith(field + '(') for s in stripped):
            stripped.append(field + '(字段撤回)')
    landed = {k: row.get(k) for k in SERVICE_FIELDS}
    return 'landed', '已入湖', landed, stripped


def ingest_payloads(lake, payloads, delivery_id, now_iso):
    store = lake.store()
    secret = lake.secret()
    results = []
    with store.tx():
        services_copy = find_detail_copy(store, with_service_id=True)
        services = lake.read_copy(services_copy)
        existing_ids = {r['service_id'] for r in services}
        landed_rows = []
        for payload in payloads:
            decision, reason, landed, stripped = _ingest_one(store, secret, payload, existing_ids)
            person_token = util.keyed(secret, 'person', str(payload.get('person_id'))) if payload.get('person_id') is not None else None
            service_token = util.keyed(secret, 'value', str(payload.get('service_id'))) if payload.get('service_id') is not None else None
            store.exec(
                'INSERT INTO ingest_log(delivery_id, at, decision, reason, person_token, service_token, detail)'
                ' VALUES (?,?,?,?,?,?,?)',
                (
                    delivery_id,
                    now_iso,
                    decision,
                    reason,
                    person_token,
                    service_token,
                    json.dumps({'stripped': stripped}, ensure_ascii=False),
                ),
            )
            if landed is not None:
                services.append(landed)
                existing_ids.add(landed['service_id'])
                landed_rows.append(landed)
            results.append({
                'service_token': service_token[:12] if service_token else None,
                'decision': decision,
                'reason': reason,
                'stripped': stripped,
            })
        if landed_rows:
            lake.write_copy(services_copy, services)
            _refresh_derived(lake, store, services)
            _bump_control_totals(lake, store, landed_rows)
    return results


def _refresh_derived(lake, store, services):
    for copy in get_copies(store):
        if copy['kind'] != 'derived':
            continue
        if copy['derivation'] == 'person_day':
            new_rows = compute_person_day(services)
            if new_rows != lake.read_copy(copy):
                lake.write_copy(copy, new_rows)


def _bump_control_totals(lake, store, landed_rows):
    for copy in get_copies(store):
        if copy['kind'] != 'aggregate':
            continue
        totals = lake.read_copy(copy)
        index = {(t['partner'], t['month']): t for t in totals}
        for row in landed_rows:
            key = (row['partner'], row['date'][:7])
            entry = index.get(key)
            if entry is None:
                entry = {'partner': key[0], 'month': key[1], 'service_count': 0, 'total_fee_fen': 0}
                totals.append(entry)
                index[key] = entry
            entry['service_count'] += 1
            entry['total_fee_fen'] += row['fee_fen']
        totals.sort(key=lambda t: (t['partner'], t['month']))
        lake.write_copy(copy, totals)
