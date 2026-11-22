"""交付与历史引用：与撤回互斥的一致交付。

收到撤回即限制新交付：交付按当时仍允许使用的完整数据构建；
存在未完成的清除运行时，交付明确停止。交付副本登记为受管副本，
纳入后续清除范围。历史引用只给已清除说明与合法保留说明，
不返回旧个人资料。
"""
from __future__ import annotations

import hashlib
import json

from . import util
from .core import find_detail_copy, get_copies, live_state


def _uncleared_requests(store, now_iso):
    now = util.parse_time(now_iso)
    uncleared = []
    for row in store.q('SELECT * FROM requests'):
        req = dict(row)
        req['fields'] = json.loads(req['fields'])
        if util.parse_time(req['received_at']) > now:
            continue
        if live_state(store, req['request_id']) in ('received', 'in_progress'):
            uncleared.append(req)
    return uncleared


def _build_rows(lake, store, secret, partner, now_iso):
    services_copy = find_detail_copy(store, with_service_id=True)
    profiles_copy = find_detail_copy(store, with_service_id=False)
    services = lake.read_copy(services_copy)
    profiles = {p['person_id']: p for p in lake.read_copy(profiles_copy)}
    uncleared = _uncleared_requests(store, now_iso)
    full_tokens = {t['person_token'] for t in store.q("SELECT person_token FROM tombstones WHERE kind='person'")}
    field_tombs = store.q("SELECT person_token, partner, field FROM tombstones WHERE kind='field'")
    out = []
    for service in services:
        if service.get('partner') != partner:
            continue
        pid = service.get('person_id')
        person_token = util.keyed(secret, 'person', pid)
        if person_token in full_tokens:
            continue
        reqs = [
            r for r in uncleared
            if r['person_id'] == pid and (r['partner'] is None or r['partner'] == partner)
        ]
        if any('*' in r['fields'] for r in reqs):
            continue
        masked = {f for r in reqs for f in r['fields'] if f != '*'}
        masked |= {
            t['field'] for t in field_tombs
            if t['person_token'] == person_token and (t['partner'] is None or t['partner'] == partner)
        }
        out.append({
            'service_id': service['service_id'],
            'person_id': pid,
            'partner': partner,
            'date': service['date'],
            'fee_fen': service['fee_fen'],
            'phone': None if 'phone' in masked else profiles.get(pid, {}).get('phone'),
        })
    out.sort(key=lambda r: r['service_id'])
    return out


def deliver(lake, delivery_id, partner, now_iso):
    store = lake.store()
    secret = lake.secret()
    with store.tx():
        if store.one('SELECT 1 AS x FROM deliveries WHERE delivery_id=?', (delivery_id,)):
            raise SystemExit(f'交付编号已存在: {delivery_id}')
        blocking = store.q(
            "SELECT q.request_id FROM runs r JOIN requests q ON q.request_id = r.request_id"
            " WHERE r.kind='purge' AND r.finished_at IS NULL AND (q.partner IS NULL OR q.partner=?)",
            (partner,),
        )
        if blocking:
            note = '撤回清除进行中（%s），交付明确停止' % ','.join(sorted({b['request_id'] for b in blocking}))
            store.exec(
                'INSERT INTO deliveries(delivery_id, partner, created_at, status, note) VALUES (?,?,?,?,?)',
                (delivery_id, partner, now_iso, 'stopped', note),
            )
            return {'delivery_id': delivery_id, 'status': 'stopped', 'note': note}
        rows = _build_rows(lake, store, secret, partner, now_iso)
        rel_path = f'lake/exports/{delivery_id}.json'
        util.write_json_atomic(lake.root / rel_path, rows)
        content_hash = hashlib.sha256((lake.root / rel_path).read_bytes()).hexdigest()
        copy_id = 'C-' + delivery_id
        parents = [c['copy_id'] for c in get_copies(store) if c['kind'] == 'detail']
        store.exec(
            'INSERT INTO copies(copy_id, path, kind, availability, identifying_fields, derivation, registered_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (copy_id, rel_path, 'export', 'available', json.dumps(['person_id', 'service_id', 'phone']), None, now_iso),
        )
        for parent in parents:
            store.exec('INSERT OR IGNORE INTO lineage(child, parent) VALUES (?,?)', (copy_id, parent))
        store.exec(
            'INSERT INTO deliveries(delivery_id, partner, created_at, status, path, row_count, content_hash)'
            ' VALUES (?,?,?,?,?,?,?)',
            (delivery_id, partner, now_iso, 'delivered', rel_path, len(rows), content_hash),
        )
        return {'delivery_id': delivery_id, 'status': 'delivered', 'rows': len(rows), 'path': rel_path}


def refer(lake, delivery_id):
    """历史引用：交付后已清除范围与合法保留说明，不含个人值。"""
    store = lake.store()
    delivery = store.one('SELECT * FROM deliveries WHERE delivery_id=?', (delivery_id,))
    if delivery is None:
        raise SystemExit(f'未知交付: {delivery_id}')
    purged = store.q(
        'SELECT DISTINCT request_id, kind, field, purged_at FROM tombstones WHERE purged_at > ? ORDER BY purged_at',
        (delivery['created_at'],),
    )
    exceptions = store.q('SELECT basis_label, fields, expires_at, status FROM exceptions ORDER BY exception_id')
    return {'delivery': delivery, 'purged_scopes': purged, 'exceptions': exceptions}


def format_refer(info):
    delivery = info['delivery']
    lines = [
        f"交付 {delivery['delivery_id']}｜合作方 {delivery['partner']}｜时间 {delivery['created_at']}",
        f"  状态 {'已交付' if delivery['status'] == 'delivered' else '已明确停止'}",
    ]
    if delivery['status'] == 'delivered':
        lines.append(f"  副本 {delivery['path']}｜行数 {delivery['row_count']}")
    if delivery.get('note'):
        lines.append(f"  说明 {delivery['note']}")
    if info['purged_scopes']:
        lines.append('  交付后内容已清除的范围：')
        for scope in info['purged_scopes']:
            field = scope['field'] or '-'
            lines.append(f"    请求 {scope['request_id']}｜类别 {scope['kind']}｜字段 {field}｜清除时间 {scope['purged_at']}")
    else:
        lines.append('  交付后无清除记录')
    active = [e for e in info['exceptions'] if e['status'] == 'active']
    if active:
        lines.append('  合法保留说明：')
        for exc in active:
            fields = ','.join(json.loads(exc['fields']))
            lines.append(f"    依据 {exc['basis_label']}｜字段 {fields}｜保留至 {exc['expires_at']}，到期继续执行清除")
    lines.append('  （本引用不含个人数据）')
    return '\n'.join(lines)
