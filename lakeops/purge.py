"""撤回清除执行：范围解析、可恢复任务、保留例外与到期续清。

可恢复语义：每次运行先登记运行记录（独立提交），工作在一个事务内完成；
文件改写幂等，数据库随事务回滚。中断后重跑接着处理欠缺的副本，
不会把暂不可达或未到期的部分汇总成已完成。
"""
from __future__ import annotations

import json

from . import util
from .core import (
    get_copies,
    get_copy,
    get_request,
    get_tasks,
    live_state,
    rebuild_derived,
)

FULL = '*'


def _kind_of(field):
    return {'person_id': 'person', 'service_id': 'service'}.get(field, 'field')


def _row_matches(row, req):
    """内容匹配：人员键一致；请求限定合作方时，行内合作方须一致（缺失按匹配处理）。"""
    if row.get('person_id') != req['person_id']:
        return False
    if req['partner'] is None:
        return True
    return row.get('partner') in (None, req['partner'])


def _topo_order(copies, edges):
    ids = {c['copy_id'] for c in copies}
    indegree = {i: 0 for i in ids}
    children = {}
    for child, parent in edges:
        if child in ids and parent in ids:
            indegree[child] += 1
            children.setdefault(parent, []).append(child)
    queue = sorted(i for i, d in indegree.items() if d == 0)
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for child in sorted(children.get(node, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
        queue.sort()
    if len(order) != len(ids):
        raise SystemExit('副本血缘存在环，无法确定清除顺序')
    by_id = {c['copy_id']: c for c in copies}
    return [by_id[i] for i in order]


def resolve_targets(store, req):
    """按登记的身份字段与血缘解析清除范围（不含控制数与受限保留）。"""
    wanted = set(req['fields'])
    full = FULL in wanted
    targets = []
    for copy in get_copies(store):
        if copy['kind'] in ('aggregate', 'restricted'):
            continue
        idf = set(copy['identifying_fields'])
        hit = bool(idf) if full else bool(wanted & idf)
        if hit:
            targets.append(copy)
    edges = [(e['child'], e['parent']) for e in store.q('SELECT child, parent FROM lineage')]
    return _topo_order(targets, edges)


def begin_run(store, request_id, now_iso):
    """登记运行记录并独立提交：中断后交付能感知未完成的清除。"""
    with store.tx():
        store.exec('UPDATE runs SET finished_at=? WHERE request_id=? AND finished_at IS NULL', (now_iso, request_id))
        run_id = util.new_id('run')
        store.exec(
            'INSERT INTO runs(run_id, kind, request_id, started_at) VALUES (?,?,?,?)',
            (run_id, 'purge', request_id, now_iso),
        )
    return run_id


def run_purge(lake, request_ids, now_iso):
    """执行（或恢复）指定请求的清除，并处理到期的保留例外。"""
    store = lake.store()
    secret = lake.secret()
    for rid in request_ids:
        if get_request(store, rid) is None:
            raise SystemExit(f'未知请求: {rid}')
    run_ids = [begin_run(store, rid, now_iso) for rid in request_ids]
    swept = []
    with store.tx():
        for rid, run_id in zip(request_ids, run_ids):
            req = get_request(store, rid)
            if FULL in req['fields']:
                _pre_pass_vouchers(lake, store, secret, req, now_iso)
            _ensure_tasks(lake, store, req, now_iso)
            _execute_tasks(lake, store, secret, req, now_iso)
            store.exec('UPDATE requests SET state=? WHERE request_id=?', (live_state(store, rid), rid))
            store.exec('UPDATE runs SET finished_at=? WHERE run_id=?', (now_iso, run_id))
        swept = _sweep_exceptions(lake, store, secret, now_iso)
        _settle_requests(store, now_iso)
    return _report(store, request_ids, swept)


def _vouchers_copy(store):
    for copy in get_copies(store):
        if copy['kind'] == 'restricted':
            return copy
    raise SystemExit('副本登记缺少受限保留副本')


def _pre_pass_vouchers(lake, store, secret, req, now_iso):
    """整人清除前提取结算保留凭据。

    凭据在改写任何明细之前写入独立受限副本；凭据只保留依据覆盖的
    受限字段，不含人员键，运行态依据登记随清除解绑。
    """
    now_date = now_iso[:10]
    auths = store.q(
        "SELECT * FROM authorities WHERE kind='settlement' AND person_id=?",
        (req['person_id'],),
    )
    if not auths:
        return
    active = [
        a for a in auths
        if a['until_date'] and a['until_date'] > now_date
        and (req['partner'] is None or a['partner'] == req['partner'])
    ]
    vouchers_copy = _vouchers_copy(store)
    existing_rows = lake.read_copy(vouchers_copy)
    existing_ids = {v['voucher_id'] for v in existing_rows}
    new_rows = []
    matched_exc = set()
    if active:
        for copy in get_copies(store):
            if copy['kind'] not in ('detail', 'export') or copy['availability'] != 'available':
                continue
            for row in lake.read_copy(copy):
                if row.get('person_id') != req['person_id']:
                    continue
                sid, fee, day = row.get('service_id'), row.get('fee_fen'), row.get('date')
                if not sid or fee is None or not day:
                    continue
                for auth in active:
                    if auth['partner'] != row.get('partner'):
                        continue
                    if not (auth['from_date'] <= day < auth['until_date']):
                        continue
                    exc_id = f"{req['request_id']}-{auth['authority_id']}"
                    matched_exc.add(exc_id)
                    voucher_id = 'V' + util.keyed(secret, 'voucher', exc_id, sid)[:16]
                    if voucher_id in existing_ids:
                        continue
                    existing_ids.add(voucher_id)
                    new_rows.append({
                        'voucher_id': voucher_id,
                        'exception_token': util.keyed(secret, 'exception', exc_id),
                        'basis_label': auth['authority_id'],
                        'service_id': sid,
                        'partner': row.get('partner'),
                        'fee_fen': fee,
                        'service_month': day[:7],
                        'retained_until': auth['until_date'],
                    })
    if new_rows:
        lake.write_copy(vouchers_copy, existing_rows + new_rows)
    for exc_id in sorted(matched_exc):
        auth = next(a for a in active if f"{req['request_id']}-{a['authority_id']}" == exc_id)
        store.exec(
            'INSERT OR IGNORE INTO exceptions(exception_id, request_id, basis_label, fields, expires_at, status, person_token)'
            ' VALUES (?,?,?,?,?,?,?)',
            (
                exc_id,
                req['request_id'],
                auth['authority_id'],
                auth['fields'],
                auth['until_date'],
                'active',
                util.keyed(secret, 'person', req['person_id']),
            ),
        )
    # 运行态依据登记与已清除身份解绑；例外记录保留依据编号、字段与期限
    for auth in auths:
        store.exec('DELETE FROM authorities WHERE authority_id=?', (auth['authority_id'],))


def _ensure_tasks(lake, store, req, now_iso):
    """为范围内副本登记任务；新登记的副本（如后续交付）在重跑时纳入。"""
    for copy in resolve_targets(store, req):
        existing = store.one(
            'SELECT status FROM tasks WHERE request_id=? AND copy_id=?',
            (req['request_id'], copy['copy_id']),
        )
        if existing:
            continue
        status = 'pending'
        if copy['availability'] == 'available' and copy['kind'] in ('detail', 'export'):
            rows = lake.read_copy(copy)
            if not any(_row_matches(row, req) for row in rows):
                status = 'no_match'
        store.exec(
            'INSERT INTO tasks(request_id, copy_id, status, updated_at) VALUES (?,?,?,?)',
            (req['request_id'], copy['copy_id'], status, now_iso),
        )


def _execute_tasks(lake, store, secret, req, now_iso):
    order = [c['copy_id'] for c in resolve_targets(store, req)]
    tasks = {t['copy_id']: t for t in get_tasks(store, req['request_id'])}
    for copy_id in order:
        task = tasks.get(copy_id)
        if task is None or task['status'] not in ('pending', 'blocked'):
            continue
        copy = get_copy(store, copy_id)
        if copy['availability'] != 'available':
            store.exec(
                "UPDATE tasks SET status='blocked', note=?, updated_at=? WHERE request_id=? AND copy_id=?",
                ('副本暂不可达', now_iso, req['request_id'], copy_id),
            )
            continue
        if copy['kind'] == 'derived':
            new_rows = rebuild_derived(lake, store, copy)
            if new_rows != lake.read_copy(copy):
                lake.write_copy(copy, new_rows)
        else:
            _purge_rows(lake, store, secret, req, copy, now_iso)
        store.exec(
            "UPDATE tasks SET status='done', note=NULL, updated_at=? WHERE request_id=? AND copy_id=?",
            (now_iso, req['request_id'], copy_id),
        )


def _add_tombstone(store, secret, value, kind, person_token, req, field, now_iso):
    store.exec(
        'INSERT OR IGNORE INTO tombstones(value_hash, kind, person_token, partner, field, request_id, purged_at)'
        ' VALUES (?,?,?,?,?,?,?)',
        (
            util.keyed(secret, 'value', value),
            kind,
            person_token,
            req['partner'],
            field,
            req['request_id'],
            now_iso,
        ),
    )


def _purge_rows(lake, store, secret, req, copy, now_iso):
    """明细/导出副本：整人清除删除行，字段撤回只置空目标字段；他人记录不动。"""
    rows = lake.read_copy(copy)
    full = FULL in req['fields']
    wanted = [f for f in req['fields'] if f != FULL and f in set(copy['identifying_fields'])]
    person_token = util.keyed(secret, 'person', req['person_id'])
    changed = False
    if full:
        kept = []
        for row in rows:
            if _row_matches(row, req):
                for field in copy['identifying_fields']:
                    value = row.get(field)
                    if isinstance(value, str) and value:
                        _add_tombstone(store, secret, value, _kind_of(field), person_token, req, field, now_iso)
                changed = True
            else:
                kept.append(row)
        rows = kept
    else:
        for row in rows:
            if not _row_matches(row, req):
                continue
            for field in wanted:
                value = row.get(field)
                if isinstance(value, str) and value:
                    _add_tombstone(store, secret, value, 'field', person_token, req, field, now_iso)
                    row[field] = None
                    changed = True
    if changed:
        lake.write_copy(copy, rows)


def _sweep_exceptions(lake, store, secret, now_iso):
    """保留期届满：继续执行原清除请求，删除到期凭据；控制数不动。"""
    now_date = now_iso[:10]
    due = store.q("SELECT * FROM exceptions WHERE status='active' AND expires_at<=?", (now_date,))
    if not due:
        return []
    vouchers_copy = _vouchers_copy(store)
    rows = lake.read_copy(vouchers_copy)
    tokens = {util.keyed(secret, 'exception', e['exception_id']) for e in due}
    kept = [v for v in rows if v.get('exception_token') not in tokens]
    if len(kept) != len(rows):
        lake.write_copy(vouchers_copy, kept)
    swept = []
    for exc in due:
        store.exec(
            "UPDATE exceptions SET status='fulfilled', fulfilled_at=? WHERE exception_id=?",
            (now_iso, exc['exception_id']),
        )
        swept.append(exc['exception_id'])
    return swept


def _settle_requests(store, now_iso):
    for row in store.q("SELECT request_id FROM requests WHERE state='cleared'"):
        active = store.one(
            "SELECT 1 AS x FROM exceptions WHERE request_id=? AND status='active'",
            (row['request_id'],),
        )
        if not active:
            store.exec("UPDATE requests SET state='settled' WHERE request_id=?", (row['request_id'],))


def _report(store, request_ids, swept):
    report = {}
    for rid in request_ids:
        report[rid] = {
            'state': live_state(store, rid),
            'tasks': {t['copy_id']: t['status'] for t in get_tasks(store, rid)},
        }
    if swept:
        report['swept_exceptions'] = swept
    return report
