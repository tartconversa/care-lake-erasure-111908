"""命令行入口：初始化、撤回登记、清除、接收后续资料、交付、查询状态、核对。

所有状态变更命令接受 --now 注入时钟；缺省使用真实当前时间。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core, deliver as deliver_mod, ingest as ingest_mod, purge as purge_mod, report, util


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _now(args):
    return args.now if args.now else util.current_iso()


def cmd_init(args):
    lake = core.Lake(args.root)
    _print_json(core.init_lake(lake, args.source, _now(args)))
    return 0


def cmd_withdraw(args):
    lake = core.Lake(args.root)
    store = lake.store()
    fields = [f.strip() for f in args.fields.split(',') if f.strip()]
    if not fields:
        raise SystemExit('字段范围不能为空；全部可识别内容用 *')
    received_at = args.received_at if args.received_at else _now(args)
    with store.tx():
        if store.one('SELECT 1 AS x FROM requests WHERE request_id=?', (args.request_id,)):
            raise SystemExit(f'请求编号已存在: {args.request_id}')
        store.exec(
            'INSERT INTO requests(request_id, person_id, partner, fields, received_at, state) VALUES (?,?,?,?,?,?)',
            (args.request_id, args.person, args.partner, json.dumps(fields, ensure_ascii=False), received_at, 'received'),
        )
    _print_json({'request_id': args.request_id, 'state': 'received', 'note': '已限制新交付，物理清除待执行'})
    return 0


def cmd_purge(args):
    lake = core.Lake(args.root)
    store = lake.store()
    if args.all:
        request_ids = [r['request_id'] for r in store.q("SELECT request_id FROM requests WHERE state != 'settled' ORDER BY request_id")]
        if not request_ids:
            request_ids = [r['request_id'] for r in store.q('SELECT request_id FROM requests ORDER BY request_id')]
    else:
        request_ids = args.request
    if not request_ids:
        raise SystemExit('请用 --request 指定请求，或用 --all')
    _print_json(purge_mod.run_purge(lake, request_ids, _now(args)))
    return 0


def cmd_ingest(args):
    lake = core.Lake(args.root)
    store = lake.store()
    if args.delivery:
        meta = store.one("SELECT value FROM meta WHERE key='source_dir'")
        incoming = util.read_json(Path(meta['value']) / 'incoming.json')
        envelope = next((e for e in incoming if e['delivery_id'] == args.delivery), None)
        if envelope is None:
            raise SystemExit(f'来源中无此投递: {args.delivery}')
        payloads = ingest_mod.normalize_payloads(envelope)
        delivery_id = args.delivery
    else:
        raw = util.read_json(args.file)
        payloads = ingest_mod.normalize_payloads(raw)
        delivery_id = raw.get('delivery_id') if isinstance(raw, dict) and raw.get('delivery_id') else f'FILE:{Path(args.file).name}'
    _print_json({'delivery_id': delivery_id, 'results': ingest_mod.ingest_payloads(lake, payloads, delivery_id, _now(args))})
    return 0


def cmd_deliver(args):
    lake = core.Lake(args.root)
    _print_json(deliver_mod.deliver(lake, args.delivery_id, args.partner, _now(args)))
    return 0


def cmd_refer(args):
    lake = core.Lake(args.root)
    print(deliver_mod.format_refer(deliver_mod.refer(lake, args.delivery_id)))
    return 0


def cmd_status(args):
    lake = core.Lake(args.root)
    reports = report.status_report(lake, args.request)
    if args.json:
        _print_json(reports)
    else:
        print(report.format_status(reports))
    return 0


def cmd_verify(args):
    lake = core.Lake(args.root)
    record, out_path = report.verify_request(lake, args.request, _now(args))
    print(
        f"请求 {args.request} 核对完成：扫描 {record['files_scanned']} 个副本，"
        f"残留 {record['residual_count']} 处，例外内保留 {record['exception_retained_count']} 处"
    )
    print(f'核对记录 {out_path}（只含散列与位置，不含个人值）')
    return 0


def cmd_drill(args):
    lake = core.Lake(args.root)
    store = lake.store()
    with store.tx():
        if store.one('SELECT 1 AS x FROM copies WHERE copy_id=?', (args.copy,)) is None:
            raise SystemExit(f'未知副本: {args.copy}')
        store.exec('UPDATE copies SET availability=? WHERE copy_id=?', (args.availability, args.copy))
    _print_json({'copy_id': args.copy, 'availability': args.availability})
    return 0


def cmd_grant(args):
    lake = core.Lake(args.root)
    store = lake.store()
    fields = [f.strip() for f in args.fields.split(',') if f.strip()]
    now_iso = _now(args)
    with store.tx():
        if store.one('SELECT 1 AS x FROM authorities WHERE authority_id=?', (args.authority_id,)):
            raise SystemExit(f'依据编号已存在: {args.authority_id}')
        store.exec(
            'INSERT INTO authorities(authority_id, person_id, partner, fields, from_date, until_date, kind, granted_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (
                args.authority_id,
                args.person,
                args.partner,
                json.dumps(fields, ensure_ascii=False),
                args.from_date,
                args.until_date,
                args.kind,
                now_iso,
            ),
        )
    _print_json({'authority_id': args.authority_id, 'granted_at': now_iso})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='lakeops', description='养老服务数据湖撤回清除与存续处理（合成资料演练）')
    sub = parser.add_subparsers(dest='cmd', required=True)

    def add_now(p):
        p.add_argument('--now', help='注入时钟（ISO 8601），缺省用真实当前时间')

    p = sub.add_parser('init', help='从来源资料包初始化湖与运行状态')
    p.add_argument('--root', required=True, help='运行目录（湖文件与状态库）')
    p.add_argument('--source', required=True, help='来源资料目录（含 catalog 的 data 目录）')
    add_now(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('withdraw', help='登记新的撤回请求（登记即限制新交付）')
    p.add_argument('--root', required=True)
    p.add_argument('--request-id', required=True)
    p.add_argument('--person', required=True)
    p.add_argument('--partner', default=None, help='缺省表示全部合作方')
    p.add_argument('--fields', required=True, help='逗号分隔字段，或 * 表示全部可识别内容')
    p.add_argument('--received-at', default=None)
    add_now(p)
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser('purge', help='执行或恢复清除，并处理到期例外')
    p.add_argument('--root', required=True)
    p.add_argument('--request', action='append', help='请求编号，可多次')
    p.add_argument('--all', action='store_true', help='处理所有未了结请求')
    add_now(p)
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser('ingest', help='接收后续资料（日增量或离线补交）')
    p.add_argument('--root', required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--delivery', help='来源 incoming.json 中的投递编号')
    group.add_argument('--file', help='投递文件（JSON）')
    add_now(p)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser('deliver', help='构建受管交付（与撤回互斥）')
    p.add_argument('--root', required=True)
    p.add_argument('--delivery-id', required=True)
    p.add_argument('--partner', required=True)
    add_now(p)
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser('refer', help='查看交付的历史引用（已清除与合法保留说明）')
    p.add_argument('--root', required=True)
    p.add_argument('--delivery-id', required=True)
    add_now(p)
    p.set_defaults(func=cmd_refer)

    p = sub.add_parser('status', help='查询处理状态（区分仅限制访问与已完成清除）')
    p.add_argument('--root', required=True)
    p.add_argument('--request', action='append', help='请求编号，可多次；缺省全部')
    p.add_argument('--json', action='store_true')
    add_now(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('verify', help='实际文件扫描核对（记录不含个人值）')
    p.add_argument('--root', required=True)
    p.add_argument('--request', required=True)
    add_now(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('drill', help='演练：调整副本可达性')
    p.add_argument('--root', required=True)
    p.add_argument('--copy', required=True)
    p.add_argument('--availability', choices=['available', 'unavailable'], required=True)
    p.set_defaults(func=cmd_drill)

    p = sub.add_parser('grant', help='登记重新建立的处理依据')
    p.add_argument('--root', required=True)
    p.add_argument('--authority-id', required=True)
    p.add_argument('--person', required=True)
    p.add_argument('--partner', required=True)
    p.add_argument('--kind', choices=['service', 'settlement'], default='service')
    p.add_argument('--fields', required=True)
    p.add_argument('--from-date', required=True)
    p.add_argument('--until-date', default=None)
    add_now(p)
    p.set_defaults(func=cmd_grant)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
