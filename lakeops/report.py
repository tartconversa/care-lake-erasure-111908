"""处理状态查询与实际文件扫描核对。

状态口径区分「仅限制访问」（已限制新交付，物理清除未完成）与
「已完成清除」（范围内副本均已改写）。核对用键控散列在实际文件上
扫描，核对记录只含散列、路径与字段名，不保存待清除的个人值。
"""
from __future__ import annotations

import json

from . import util
from .core import COPY_KIND_LABELS, get_copies, get_exceptions, get_request, get_requests, get_tasks, live_state

STATE_LABELS = {
    'received': '仅限制访问（已收到撤回并限制新交付，物理清除未开始）',
    'in_progress': '仅限制访问（清除进行中，物理清除未完成）',
    'cleared': '已完成清除（范围内副本均已改写，剩余例外待期履行）',
    'settled': '已完成清除（已了结，无剩余例外）',
}

TASK_LABELS = {
    'done': '已清除',
    'pending': '待处理',
    'blocked': '暂不可达',
    'no_match': '无匹配内容',
}


def status_report(lake, request_ids=None):
    store = lake.store()
    requests = get_requests(store)
    if request_ids:
        wanted = set(request_ids)
        requests = [r for r in requests if r['request_id'] in wanted]
        missing = wanted - {r['request_id'] for r in requests}
        if missing:
            raise SystemExit('未知请求: ' + ','.join(sorted(missing)))
    copies = {c['copy_id']: c for c in get_copies(store)}
    reports = []
    for req in requests:
        state = live_state(store, req['request_id'])
        tasks = []
        pending_locations = []
        for task in get_tasks(store, req['request_id']):
            copy = copies.get(task['copy_id'])
            entry = {
                'copy_id': task['copy_id'],
                'path': copy['path'] if copy else None,
                'kind': copy['kind'] if copy else None,
                'status': task['status'],
                'note': task['note'],
            }
            tasks.append(entry)
            if task['status'] in ('pending', 'blocked'):
                pending_locations.append({
                    'copy_id': task['copy_id'],
                    'path': entry['path'],
                    'reason': task['note'] or '待处理',
                })
        exceptions = [
            {
                'basis_label': e['basis_label'],
                'fields': e['fields'],
                'expires_at': e['expires_at'],
                'status': e['status'],
            }
            for e in get_exceptions(store, req['request_id'])
        ]
        reports.append({
            'request_id': req['request_id'],
            'person_id': req['person_id'],
            'partner': req['partner'],
            'fields': req['fields'],
            'received_at': req['received_at'],
            'state': state,
            'state_label': STATE_LABELS[state],
            'tasks': tasks,
            'pending_locations': pending_locations,
            'exceptions': exceptions,
            'last_verification': _last_verification(lake, req['request_id']),
        })
    return reports


def _last_verification(lake, request_id):
    if not lake.verify_dir.exists():
        return None
    files = sorted(lake.verify_dir.glob(f'{request_id}-*.json'))
    if not files:
        return None
    record = util.read_json(files[-1])
    return {
        'verified_at': record['verified_at'],
        'files_scanned': record['files_scanned'],
        'residual_count': record['residual_count'],
        'exception_retained_count': record['exception_retained_count'],
        'record': str(files[-1].relative_to(lake.root)),
    }


def format_status(reports):
    lines = []
    for rep in reports:
        partner = rep['partner'] if rep['partner'] is not None else '全部'
        fields = ','.join(rep['fields'])
        lines.append(f"请求 {rep['request_id']}｜人员 {rep['person_id']}｜合作方 {partner}｜字段 {fields}")
        lines.append(f"  收到时间 {rep['received_at']}")
        lines.append(f"  当前状态 {rep['state_label']}")
        if rep['tasks']:
            lines.append('  副本处理：')
            for task in rep['tasks']:
                kind = COPY_KIND_LABELS.get(task['kind'], task['kind'] or '-')
                status = TASK_LABELS.get(task['status'], task['status'])
                note = f"（{task['note']}）" if task['note'] else ''
                lines.append(f"    {task['copy_id']} {kind} {task['path']} — {status}{note}")
        if rep['pending_locations']:
            lines.append('  未完成位置：')
            for loc in rep['pending_locations']:
                lines.append(f"    {loc['copy_id']} {loc['path']}（{loc['reason']}）")
        if rep['exceptions']:
            lines.append('  剩余例外：')
            for exc in rep['exceptions']:
                fields_text = ','.join(exc['fields'])
                state_text = '生效中' if exc['status'] == 'active' else '已履行'
                lines.append(f"    依据 {exc['basis_label']}｜字段 {fields_text}｜到期 {exc['expires_at']}｜{state_text}")
        else:
            lines.append('  剩余例外：无')
        verification = rep['last_verification']
        if verification:
            lines.append(
                '  最近核对：{verified_at} 扫描 {files_scanned} 个副本，残留 {residual_count} 处，'
                '例外内保留 {exception_retained_count} 处（记录 {record}）'.format(**verification)
            )
        else:
            lines.append('  最近核对：无')
    return '\n'.join(lines)


def verify_request(lake, request_id, now_iso):
    """实际文件扫描核对：只比对键控散列，核对记录不含个人值。"""
    store = lake.store()
    secret = lake.secret()
    if get_request(store, request_id) is None:
        raise SystemExit(f'未知请求: {request_id}')
    tombs = store.q('SELECT * FROM tombstones WHERE request_id=?', (request_id,))
    by_hash = {t['value_hash']: t for t in tombs}
    covered = set()
    for exc in get_exceptions(store, request_id):
        if exc['status'] == 'active':
            covered |= set(exc['fields'])
    matches = []
    scanned = 0
    for copy in get_copies(store):
        rows = lake.read_copy(copy)
        scanned += 1
        for path, value in util.walk_strings(rows):
            tomb = by_hash.get(util.keyed(secret, 'value', value))
            if tomb is None:
                continue
            disposition = '残留'
            if copy['kind'] == 'restricted' and tomb['field'] in covered:
                disposition = '例外内保留'
            matches.append({
                'copy_id': copy['copy_id'],
                'path': copy['path'],
                'at': path,
                'field': tomb['field'],
                'kind': tomb['kind'],
                'value_hash12': tomb['value_hash'][:12],
                'disposition': disposition,
            })
    residual = [m for m in matches if m['disposition'] == '残留']
    record = {
        'request_id': request_id,
        'verified_at': now_iso,
        'files_scanned': scanned,
        'residual_count': len(residual),
        'exception_retained_count': len(matches) - len(residual),
        'matches': matches,
    }
    lake.verify_dir.mkdir(parents=True, exist_ok=True)
    out_path = lake.verify_dir / f'{request_id}-{util.stamp(now_iso)}.json'
    util.write_json_atomic(out_path, record)
    return record, out_path
