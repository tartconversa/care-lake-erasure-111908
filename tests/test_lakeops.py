"""业务演练测试：撤回清除、到期续清、后续接收、交付互斥与核对。"""
from pathlib import Path
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout

from lakeops import core, deliver as deliver_mod, ingest as ingest_mod, purge, report
from lakeops.cli import main as cli_main
from lakeops.util import read_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data'

T0 = '2026-11-05T09:00:00+08:00'   # 初始化
T1 = '2026-11-07T09:00:00+08:00'  # 首次清除（导出暂不可达）
T2 = '2026-11-08T09:00:00+08:00'  # 恢复后清除
T3 = '2026-11-11T09:00:00+08:00'  # 重授权窗口内
T4 = '2026-11-21T09:00:00+08:00'  # 保留期届满后


class LakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.lake = core.Lake(self.root)
        core.init_lake(self.lake, SOURCE, T0)

    def lake_json(self, rel):
        return read_json(self.root / rel)

    def lake_text(self, rel):
        return (self.root / rel).read_text(encoding='utf-8')

    def store(self):
        return self.lake.store()

    def drill(self, copy_id, availability):
        store = self.store()
        with store.tx():
            store.exec('UPDATE copies SET availability=? WHERE copy_id=?', (availability, copy_id))


class InitTest(LakeCase):
    def test_init_builds_lake(self):
        self.assertEqual(len(self.lake_json('lake/detail/profiles.json')), 6)
        self.assertEqual(len(self.lake_json('lake/detail/services.json')), 18)
        self.assertEqual(len(self.lake_json('lake/derived/person_day.json')), 18)
        totals = self.lake_json('lake/derived/control_totals.json')
        self.assertEqual({(t['partner'], t['month']): t['service_count'] for t in totals},
                         {('A', '2026-10'): 9, ('B', '2026-10'): 9})
        self.assertEqual(self.lake_json('lake/restricted/settlement_vouchers.json'), [])
        store = self.store()
        copies = {c['copy_id']: c for c in core.get_copies(store)}
        self.assertEqual(copies['C3']['availability'], 'unavailable')
        self.assertEqual(copies['C4']['derivation'], 'person_day')
        self.assertEqual(core.live_state(store, 'E1'), 'received')
        self.assertEqual(core.live_state(store, 'E2'), 'received')


class FieldPurgeTest(LakeCase):
    def test_field_purge_shared_file_and_blocked_export(self):
        purge.run_purge(self.lake, ['E1'], T1)
        profiles = {p['person_id']: p for p in self.lake_json('lake/detail/profiles.json')}
        self.assertIsNone(profiles['P1']['phone'])
        self.assertEqual(profiles['P1']['name'], '合成人员1')
        self.assertEqual(profiles['P2']['phone'], '00000000002')
        # 共享文件中的其他人员记录不受影响
        self.assertEqual(len(profiles), 6)
        # 导出副本暂不可达：未改写，任务挂起，请求不能汇总成已完成
        self.assertIn('00000000001', self.lake_text('lake/exports/managed_export.json'))
        store = self.store()
        self.assertEqual(core.live_state(store, 'E1'), 'in_progress')
        rep = report.status_report(self.lake, ['E1'])[0]
        self.assertIn('仅限制访问', rep['state_label'])
        self.assertEqual([l['copy_id'] for l in rep['pending_locations']], ['C3'])
        # 服务明细与派生表无 phone 字段，不在范围内
        self.assertEqual(len(self.lake_json('lake/detail/services.json')), 18)

    def test_resume_after_export_reachable(self):
        purge.run_purge(self.lake, ['E1'], T1)
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E1'], T2)
        export = self.lake_json('lake/exports/managed_export.json')
        p1_rows = [r for r in export if r['person_id'] == 'P1']
        self.assertEqual(len(p1_rows), 2)
        self.assertTrue(all(r['phone'] is None for r in p1_rows))
        p2_rows = [r for r in export if r['person_id'] == 'P2']
        self.assertTrue(all(r['phone'] == '00000000002' for r in p2_rows))
        self.assertNotIn('00000000001', self.lake_text('lake/exports/managed_export.json'))
        store = self.store()
        self.assertEqual(core.live_state(store, 'E1'), 'settled')
        rep = report.status_report(self.lake, ['E1'])[0]
        self.assertIn('已完成清除', rep['state_label'])
        # 重跑幂等：文件内容不变，状态不变
        before = self.lake_text('lake/exports/managed_export.json')
        purge.run_purge(self.lake, ['E1'], T2)
        self.assertEqual(self.lake_text('lake/exports/managed_export.json'), before)
        self.assertEqual(core.live_state(self.store(), 'E1'), 'settled')


class FullErasureTest(LakeCase):
    def test_full_erasure_with_settlement_exception(self):
        purge.run_purge(self.lake, ['E2'], T1)
        profiles = self.lake_json('lake/detail/profiles.json')
        self.assertNotIn('P2', {p['person_id'] for p in profiles})
        services = self.lake_json('lake/detail/services.json')
        self.assertEqual(len(services), 15)
        self.assertNotIn('P2', {s['person_id'] for s in services})
        person_day = self.lake_json('lake/derived/person_day.json')
        self.assertNotIn('P2', {r['person_id'] for r in person_day})
        # 结算凭据独立保留：依据覆盖字段齐全，不含人员键，不随整人消失
        vouchers = self.lake_json('lake/restricted/settlement_vouchers.json')
        self.assertEqual({v['service_id'] for v in vouchers}, {'S2', 'S8', 'S14'})
        for v in vouchers:
            self.assertNotIn('person_id', v)
            self.assertEqual(v['basis_label'], 'R1')
            self.assertEqual(v['retained_until'], '2026-11-20')
            self.assertEqual(v['service_month'], '2026-10')
        self.assertNotIn('P2', self.lake_text('lake/restricted/settlement_vouchers.json'))
        # 去身份控制数保留，不随清除回减
        totals = self.lake_json('lake/derived/control_totals.json')
        self.assertEqual({(t['partner'], t['month']): (t['service_count'], t['total_fee_fen']) for t in totals},
                         {('A', '2026-10'): (9, 5310), ('B', '2026-10'): (9, 5400)})
        # 例外说明具体依据、覆盖字段与到期
        store = self.store()
        exceptions = core.get_exceptions(store, 'E2')
        self.assertEqual(len(exceptions), 1)
        self.assertEqual(exceptions[0]['basis_label'], 'R1')
        self.assertEqual(exceptions[0]['fields'], ['service_id', 'fee_fen'])
        self.assertEqual(exceptions[0]['expires_at'], '2026-11-20')
        # 运行态依据登记已与身份解绑
        self.assertIsNone(store.one("SELECT 1 AS x FROM authorities WHERE authority_id='R1'"))
        # 导出暂不可达：请求未完成
        self.assertEqual(core.live_state(store, 'E2'), 'in_progress')
        rep = report.status_report(self.lake, ['E2'])[0]
        self.assertEqual([l['copy_id'] for l in rep['pending_locations']], ['C3'])
        self.assertEqual(len(rep['exceptions']), 1)

    def test_erasure_completes_when_export_reachable(self):
        purge.run_purge(self.lake, ['E2'], T1)
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E2'], T2)
        export = self.lake_json('lake/exports/managed_export.json')
        self.assertNotIn('P2', {r['person_id'] for r in export})
        self.assertEqual(len(export), 10)
        store = self.store()
        self.assertEqual(core.live_state(store, 'E2'), 'cleared')
        rep = report.status_report(self.lake, ['E2'])[0]
        self.assertIn('已完成清除', rep['state_label'])
        self.assertEqual(len(rep['exceptions']), 1)

    def test_exception_expiry_continues_purge(self):
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E2'], T1)
        self.assertEqual(len(self.lake_json('lake/restricted/settlement_vouchers.json')), 3)
        # 保留期届满后续清：凭据删除，控制数仍在
        purge.run_purge(self.lake, ['E2'], T4)
        self.assertEqual(self.lake_json('lake/restricted/settlement_vouchers.json'), [])
        totals = self.lake_json('lake/derived/control_totals.json')
        self.assertEqual(len(totals), 2)
        store = self.store()
        self.assertEqual(core.live_state(store, 'E2'), 'settled')
        exceptions = core.get_exceptions(store, 'E2')
        self.assertEqual(exceptions[0]['status'], 'fulfilled')
        rep = report.status_report(self.lake, ['E2'])[0]
        self.assertIn('已完成清除', rep['state_label'])

    def test_interrupted_run_resume_keeps_valid_persons(self):
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E2'], T1)
        # 模拟“文件已改写、进度未提交”的中断：任务回到待处理
        store = self.store()
        with store.tx():
            store.exec("UPDATE tasks SET status='pending' WHERE request_id='E2' AND copy_id='C3'")
            store.exec("UPDATE requests SET state='in_progress' WHERE request_id='E2'")
        purge.run_purge(self.lake, ['E2'], T2)
        export = self.lake_json('lake/exports/managed_export.json')
        self.assertNotIn('P2', {r['person_id'] for r in export})
        # 仍有效人员的数据未被破坏
        persons = {r['person_id'] for r in export}
        self.assertEqual(persons, {'P1', 'P3', 'P4', 'P5', 'P6'})
        self.assertEqual(core.live_state(self.store(), 'E2'), 'cleared')


class IngestTest(LakeCase):
    def test_late_offline_packages(self):
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E1'], T1)
        purge.run_purge(self.lake, ['E2'], T1)
        # D1：撤回后补到的旧服务键，幂等跳过
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S1', 'person_id': 'P1', 'partner': 'A',
                         'date': '2026-10-01', 'fee_fen': 510}], 'D1', T1)[0]
        self.assertEqual(result['decision'], 'skipped')
        self.assertEqual(len(self.lake_json('lake/detail/services.json')), 15)
        # D2：重授权窗口内的新服务，放行
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'NEW1', 'person_id': 'P1', 'partner': 'A',
                         'date': '2026-11-11', 'fee_fen': 510}], 'D2', T3)[0]
        self.assertEqual(result['decision'], 'landed')
        services = self.lake_json('lake/detail/services.json')
        self.assertIn('NEW1', {s['service_id'] for s in services})
        person_day = self.lake_json('lake/derived/person_day.json')
        self.assertIn(('P1', '2026-11-11'), {(r['person_id'], r['date']) for r in person_day})
        totals = self.lake_json('lake/derived/control_totals.json')
        self.assertIn(('A', '2026-11'), {(t['partner'], t['month']) for t in totals})

    def test_purged_content_never_resurrects(self):
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E1'], T1)
        purge.run_purge(self.lake, ['E2'], T1)
        # 已清除人员整行拒收
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S99', 'person_id': 'P2', 'partner': 'B',
                         'date': '2026-10-20', 'fee_fen': 100}], 'D9', T1)[0]
        self.assertEqual(result['decision'], 'rejected')
        # 已清除服务键不得复用
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S2', 'person_id': 'P2', 'partner': 'B',
                         'date': '2026-10-02', 'fee_fen': 520}], 'D9', T1)[0]
        self.assertEqual(result['decision'], 'rejected')
        # 撤回字段值随离线包回流时被剔除，不重新入湖
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S100', 'person_id': 'P1', 'partner': 'A',
                         'date': '2026-10-20', 'fee_fen': 100, 'phone': '00000000001'}], 'D9', T1)[0]
        self.assertEqual(result['decision'], 'landed')
        self.assertIn('phone(撤回值)', result['stripped'])
        self.assertNotIn('00000000001', self.lake_text('lake/detail/services.json'))
        # 依据空窗期内的服务拒收
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S101', 'person_id': 'P1', 'partner': 'A',
                         'date': '2026-11-06', 'fee_fen': 100}], 'D9', T3)[0]
        self.assertEqual(result['decision'], 'rejected')
        self.assertEqual(result['reason'], '无有效处理依据')
        # 接收日志不含个人值
        store = self.store()
        log_text = json.dumps(store.q('SELECT * FROM ingest_log'), ensure_ascii=False)
        self.assertNotIn('P2', log_text)
        self.assertNotIn('00000000001', log_text)

    def test_reauthorization_allows_only_window(self):
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E2'], T1)
        store = self.store()
        with store.tx():
            store.exec(
                "INSERT INTO authorities(authority_id, person_id, partner, fields, from_date, until_date, kind, granted_at)"
                " VALUES ('A9','P2','B','[\"service_id\",\"fee_fen\"]','2026-11-15','2026-12-01','service',?)",
                ('2026-11-15T09:00:00+08:00',),
            )
        # 窗口内新服务放行
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S200', 'person_id': 'P2', 'partner': 'B',
                         'date': '2026-11-16', 'fee_fen': 100}], 'D10', '2026-11-16T09:00:00+08:00')[0]
        self.assertEqual(result['decision'], 'landed')
        # 已清除的历史服务键不得复活
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S2', 'person_id': 'P2', 'partner': 'B',
                         'date': '2026-11-16', 'fee_fen': 520}], 'D10', '2026-11-16T09:00:00+08:00')[0]
        self.assertEqual(result['decision'], 'rejected')
        # 窗口外历史数据不得借重授权复活
        result = ingest_mod.ingest_payloads(
            self.lake, [{'service_id': 'S201', 'person_id': 'P2', 'partner': 'B',
                         'date': '2026-10-05', 'fee_fen': 100}], 'D10', '2026-11-16T09:00:00+08:00')[0]
        self.assertEqual(result['decision'], 'rejected')


class DeliveryTest(LakeCase):
    def test_delivery_stops_during_unfinished_purge(self):
        store = self.store()
        purge.begin_run(store, 'E2', T1)  # 模拟中断的清除运行
        result = deliver_mod.deliver(self.lake, 'X1', 'B', T1)
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse((self.root / 'lake/exports/X1.json').exists())

    def test_delivery_applies_restrictions_when_allowed(self):
        purge.run_purge(self.lake, ['E2'], T1)  # C3 暂不可达，E2 进行中
        result = deliver_mod.deliver(self.lake, 'X2', 'B', T2)
        self.assertEqual(result['status'], 'delivered')
        rows = self.lake_json('lake/exports/X2.json')
        # 已撤回人员不出现；其余合作方人员完整
        self.assertNotIn('P2', {r['person_id'] for r in rows})
        self.assertEqual({r['person_id'] for r in rows}, {'P4', 'P6'})

    def test_delivered_copy_joins_purge_scope_and_refer(self):
        result = deliver_mod.deliver(self.lake, 'X3', 'B', '2026-11-04T09:00:00+08:00')
        self.assertEqual(result['rows'], 9)  # 撤回收到前：完整数据
        self.assertIn('P2', {r['person_id'] for r in self.lake_json('lake/exports/X3.json')})
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E2'], T2)
        # 已交付但仍受管理的导出副本纳入清除范围
        rows = self.lake_json('lake/exports/X3.json')
        self.assertNotIn('P2', {r['person_id'] for r in rows})
        self.assertEqual(len(rows), 6)
        # 历史引用：显示已清除与合法保留说明，不返回旧个人资料
        info = deliver_mod.refer(self.lake, 'X3')
        text = deliver_mod.format_refer(info)
        self.assertIn('内容已清除', text)
        self.assertIn('合法保留说明', text)
        self.assertNotIn('P2', text)
        self.assertNotIn('00000000002', text)


class VerifyTest(LakeCase):
    def test_verify_scan_and_record_hygiene(self):
        purge.run_purge(self.lake, ['E1'], T1)
        # 导出暂不可达时核对：发现残留
        record, _ = report.verify_request(self.lake, 'E1', T1)
        self.assertEqual(record['residual_count'], 2)
        self.drill('C3', 'available')
        purge.run_purge(self.lake, ['E1'], T2)
        purge.run_purge(self.lake, ['E2'], T2)
        # 整人清除后：无残留，凭据属例外内保留
        record, out_path = report.verify_request(self.lake, 'E2', T2)
        self.assertEqual(record['residual_count'], 0)
        self.assertEqual(record['exception_retained_count'], 3)
        text = out_path.read_text(encoding='utf-8')
        self.assertNotIn('P2', text)
        self.assertNotIn('S2', text)
        self.assertNotIn('00000000002', text)
        # 例外到期续清后：例外内保留也归零
        purge.run_purge(self.lake, ['E2'], T4)
        record, _ = report.verify_request(self.lake, 'E2', T4)
        self.assertEqual(record['residual_count'], 0)
        self.assertEqual(record['exception_retained_count'], 0)
        rep = report.status_report(self.lake, ['E2'])[0]
        self.assertEqual(rep['last_verification']['residual_count'], 0)


class CliTest(LakeCase):
    def test_cli_entries(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['purge', '--root', str(self.root), '--request', 'E1', '--now', T1])
        self.assertIn('in_progress', buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['status', '--root', str(self.root), '--request', 'E1'])
        self.assertIn('仅限制访问', buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['ingest', '--root', str(self.root), '--delivery', 'D2', '--now', T3])
        self.assertIn('landed', buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['verify', '--root', str(self.root), '--request', 'E1', '--now', T2])
        self.assertIn('核对完成', buf.getvalue())

    def test_cli_init_and_withdraw(self):
        other = Path(self.tmp.name) / 'runtime2'
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['init', '--root', str(other), '--source', str(SOURCE), '--now', T0])
        self.assertIn('initialized_at', buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(['withdraw', '--root', str(other), '--request-id', 'E9', '--person', 'P4',
                      '--fields', '*', '--received-at', T1])
        self.assertIn('received', buf.getvalue())
        with redirect_stdout(io.StringIO()):
            cli_main(['purge', '--root', str(other), '--request', 'E9', '--now', T2])
        lake = core.Lake(other)
        self.assertNotIn('P4', {p['person_id'] for p in read_json(other / 'lake/detail/profiles.json')})


if __name__ == '__main__':
    unittest.main()
