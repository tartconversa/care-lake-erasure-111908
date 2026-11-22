"""湖运行时布局、初始化、副本登记与派生重建。

清除范围由副本登记（血缘边 + 声明身份字段）与实际内容扫描决定，
不按文件名猜测。派生表按登记的上游血缘重建。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import util
from .store import Store

SERVICE_FIELDS = ('service_id', 'person_id', 'partner', 'date', 'fee_fen')

COPY_KIND_LABELS = {
    'detail': '明细',
    'derived': '派生',
    'export': '导出',
    'aggregate': '控制数',
    'restricted': '受限保留',
}

# 来源副本在湖内的落位（初始化接线，运行时不再按路径推断）
SOURCE_COPY_LAYOUT = {
    'C1': 'lake/detail/profiles.json',
    'C2': 'lake/detail/services.json',
    'C3': 'lake/exports/managed_export.json',
}

AVAILABILITY_MAP = {'available': 'available', 'initially_offline': 'unavailable'}


class Lake:
    def __init__(self, root):
        self.root = Path(root)

    @property
    def state_dir(self):
        return self.root / 'state'

    @property
    def db_path(self):
        return self.state_dir / 'state.db'

    @property
    def secret_path(self):
        return self.state_dir / 'secret.key'

    @property
    def verify_dir(self):
        return self.state_dir / 'verifications'

    def secret(self):
        return util.load_secret(self.secret_path)

    def store(self):
        return Store.open(self.db_path)

    def copy_abs(self, copy_row):
        return self.root / copy_row['path']

    def read_copy(self, copy_row):
        return util.read_json(self.copy_abs(copy_row))

    def write_copy(self, copy_row, rows):
        util.write_json_atomic(self.copy_abs(copy_row), rows)


# ---------- 登记查询 ----------

def _parse_copy(row):
    row = dict(row)
    row['identifying_fields'] = json.loads(row['identifying_fields'])
    return row


def get_copies(store):
    return [_parse_copy(r) for r in store.q('SELECT * FROM copies ORDER BY copy_id')]


def get_copy(store, copy_id):
    row = store.one('SELECT * FROM copies WHERE copy_id=?', (copy_id,))
    return _parse_copy(row) if row else None


def find_detail_copy(store, with_service_id):
    """按声明的身份字段找明细副本，不按文件名。"""
    for c in get_copies(store):
        if c['kind'] != 'detail':
            continue
        if ('service_id' in c['identifying_fields']) == with_service_id:
            return c
    raise SystemExit('副本登记缺少所需明细副本')


def get_request(store, request_id):
    row = store.one('SELECT * FROM requests WHERE request_id=?', (request_id,))
    if row is None:
        return None
    row = dict(row)
    row['fields'] = json.loads(row['fields'])
    return row


def get_requests(store):
    return [get_request(store, r['request_id']) for r in store.q('SELECT request_id FROM requests ORDER BY request_id')]


def get_tasks(store, request_id):
    return store.q('SELECT * FROM tasks WHERE request_id=? ORDER BY copy_id', (request_id,))


def get_exceptions(store, request_id=None):
    if request_id is None:
        rows = store.q('SELECT * FROM exceptions ORDER BY exception_id')
    else:
        rows = store.q('SELECT * FROM exceptions WHERE request_id=? ORDER BY exception_id', (request_id,))
    for row in rows:
        row['fields'] = json.loads(row['fields'])
    return rows


def live_state(store, request_id):
    """按任务与例外实况计算状态，不信任汇总字段。

    received    仅限制访问：已收到撤回，物理清除未开始
    in_progress 仅限制访问：清除进行中，存在未完成位置
    cleared     已完成清除：范围内副本均已改写，仍有生效中的保留例外
    settled     已完成清除（已了结）：无剩余例外
    """
    tasks = store.q('SELECT status FROM tasks WHERE request_id=?', (request_id,))
    if not tasks:
        return 'received'
    if all(t['status'] in ('done', 'no_match') for t in tasks):
        active = store.one("SELECT 1 AS x FROM exceptions WHERE request_id=? AND status='active'", (request_id,))
        return 'cleared' if active else 'settled'
    return 'in_progress'


# ---------- 派生计算 ----------

def compute_person_day(services):
    """人员日表：由服务明细派生，含人员键，属于清除范围。"""
    agg = {}
    for row in services:
        key = (row['person_id'], row['date'])
        count, total = agg.get(key, (0, 0))
        agg[key] = (count + 1, total + row['fee_fen'])
    return [
        {'person_id': person, 'date': day, 'service_count': count, 'total_fee_fen': total}
        for (person, day), (count, total) in sorted(agg.items())
    ]


def compute_control_totals(services):
    """去身份控制数：按合作方与月份保留计数与金额，不含身份字段。"""
    agg = {}
    for row in services:
        key = (row['partner'], row['date'][:7])
        count, total = agg.get(key, (0, 0))
        agg[key] = (count + 1, total + row['fee_fen'])
    return [
        {'partner': partner, 'month': month, 'service_count': count, 'total_fee_fen': total}
        for (partner, month), (count, total) in sorted(agg.items())
    ]


def rebuild_derived(lake, store, copy_row):
    """按登记的上游血缘重算派生副本。"""
    parents = [
        get_copy(store, edge['parent'])
        for edge in store.q('SELECT parent FROM lineage WHERE child=? ORDER BY parent', (copy_row['copy_id'],))
    ]
    rows = []
    for parent in parents:
        rows.extend(lake.read_copy(parent))
    if copy_row['derivation'] == 'person_day':
        return compute_person_day(rows)
    raise SystemExit(f"未知派生方式: {copy_row['derivation']}")


# ---------- 初始化 ----------

def init_lake(lake, source_dir, now_iso):
    """从来源资料包构建湖布局与运行状态。重复初始化直接报错。"""
    source_dir = Path(source_dir)
    if lake.db_path.exists():
        raise SystemExit(f'运行目录已初始化: {lake.root}')
    lake.state_dir.mkdir(parents=True, exist_ok=True)
    util.load_secret(lake.secret_path)
    store = Store.create(lake.db_path)

    profiles = util.read_json(source_dir / 'profiles.json')
    services = util.read_json(source_dir / 'services.json')
    authorities = util.read_json(source_dir / 'authorities.json')
    requests = util.read_json(source_dir / 'requests.json')
    managed_export = util.read_json(source_dir / 'managed_export.json')
    source_copies = util.read_json(source_dir / 'copies.json')

    source_rows = {'C1': profiles, 'C2': services, 'C3': managed_export}
    for copy_id, rel_path in SOURCE_COPY_LAYOUT.items():
        util.write_json_atomic(lake.root / rel_path, source_rows[copy_id])
    util.write_json_atomic(lake.root / 'lake/derived/person_day.json', compute_person_day(services))
    util.write_json_atomic(lake.root / 'lake/derived/control_totals.json', compute_control_totals(services))
    util.write_json_atomic(lake.root / 'lake/restricted/settlement_vouchers.json', [])

    with store.tx():
        store.exec('INSERT INTO meta(key, value) VALUES (?,?)', ('schema_version', '1'))
        store.exec('INSERT INTO meta(key, value) VALUES (?,?)', ('source_dir', str(source_dir.resolve())))
        store.exec('INSERT INTO meta(key, value) VALUES (?,?)', ('initialized_at', now_iso))
        for entry in source_copies:
            copy_id = entry['copy_id']
            store.exec(
                'INSERT INTO copies(copy_id, path, kind, availability, identifying_fields, derivation, registered_at)'
                ' VALUES (?,?,?,?,?,?,?)',
                (
                    copy_id,
                    SOURCE_COPY_LAYOUT[copy_id],
                    'export' if copy_id == 'C3' else 'detail',
                    AVAILABILITY_MAP.get(entry['availability'], 'unavailable'),
                    json.dumps(entry['identifying_fields'], ensure_ascii=False),
                    None,
                    now_iso,
                ),
            )
            for parent in entry['parents']:
                store.exec('INSERT INTO lineage(child, parent) VALUES (?,?)', (copy_id, parent))
        extra_copies = [
            ('C4', 'lake/derived/person_day.json', 'derived', ['person_id'], 'person_day', ['C2']),
            ('C5', 'lake/derived/control_totals.json', 'aggregate', [], None, ['C2']),
            ('C6', 'lake/restricted/settlement_vouchers.json', 'restricted', ['service_id'], None, ['C2']),
        ]
        for copy_id, rel_path, kind, idf, derivation, parents in extra_copies:
            store.exec(
                'INSERT INTO copies(copy_id, path, kind, availability, identifying_fields, derivation, registered_at)'
                ' VALUES (?,?,?,?,?,?,?)',
                (copy_id, rel_path, kind, 'available', json.dumps(idf, ensure_ascii=False), derivation, now_iso),
            )
            for parent in parents:
                store.exec('INSERT INTO lineage(child, parent) VALUES (?,?)', (copy_id, parent))
        for auth in authorities:
            store.exec(
                'INSERT INTO authorities(authority_id, person_id, partner, fields, from_date, until_date, kind, granted_at)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (
                    auth['authority_id'],
                    auth['person_id'],
                    auth['partner'],
                    json.dumps(auth['fields'], ensure_ascii=False),
                    auth['from_date'],
                    auth['until_date'],
                    auth['kind'],
                    auth['from_date'],
                ),
            )
        for req in requests:
            store.exec(
                'INSERT INTO requests(request_id, person_id, partner, fields, received_at, state) VALUES (?,?,?,?,?,?)',
                (
                    req['request_id'],
                    req['person_id'],
                    req['partner'],
                    json.dumps(req['fields'], ensure_ascii=False),
                    req['received_at'],
                    'received',
                ),
            )
    return {
        'root': str(lake.root),
        'copies': len(SOURCE_COPY_LAYOUT) + len(extra_copies),
        'requests': [r['request_id'] for r in requests],
        'initialized_at': now_iso,
    }
