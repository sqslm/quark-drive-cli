"""Durable queue boundaries, credential isolation and share reauthorization."""

import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path

from quark_cli.desktop.service import DesktopService
from quark_cli.desktop.tasks import TIMING_FIELDS, TaskStore
from quark_cli.downloader.preferences import Preferences, save_preferences
from quark_cli.transfer import DownloadStopped
from quark_cli.transfer_progress import ProgressEvent
from quark_cli.recursive import RecursiveTransfer
from tests.test_desktop_service import Backend


class QueueBackend(Backend):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum = 0

    def download(self, request, progress, stopped):
        self.requests.append(request)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        self.release.wait(3)
        self.active -= 1
        if stopped():
            raise DownloadStopped("暂停")
        return {"size": 3}

    @contextmanager
    def _client(self):
        class Client:
            def auth_status(self):
                return {"authenticated": True}

        yield Client()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.backend = QueueBackend()
        self.service = DesktopService(
            self.messages.append,
            backend=self.backend,
            directory=Path(self.tmp.name) / "state",
        )
        self.service.saved_cookie = "sid=queue-credential"
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.backend.release.set)
        save_preferences(self.service.preferences_path, Preferences(self.tmp.name))

    def queue(self, fid, **options):
        return self.service.dispatch(
            "d",
            "download",
            {
                "entries": [{"fid": fid, "name": fid + ".txt", "directory": False}],
                **options,
            },
            None,
        )

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.service.busy and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertFalse(self.service.busy)

    def test_record_times_and_cumulative_duration_exclude_waiting_and_pause(self):
        wall, steady = [1792140000.0], [10.0]
        with patch('quark_cli.desktop.service.time.time',side_effect=lambda:wall[0]), \
             patch('quark_cli.desktop.service.time.monotonic',side_effect=lambda:steady[0]):
            self.queue('dated');self.assertTrue(self.backend.entered.wait(1))
            self.queue('pending')
            first, pending = self.service.tasks
            created=first['created_at']
            self.assertEqual(first['started_at'],created)
            self.assertIsNone(pending['started_at'])
            self.service.dispatch('cancel','tasks.cancel',{'ids':[pending['id']]},None)
            cancelled_at=pending['finished_at']
            wall[0]+=90;steady[0]+=4
            self.assertEqual(self.service.public_task(first)['elapsed_seconds'],4)
            self.service.dispatch('pause','tasks.pause',{'ids':[first['id']]},None)
            self.backend.release.set();self.service.transfers.submit(lambda:None).result(timeout=3)
            self.assertEqual((first['state'],first['elapsed_seconds']),('stopped',4))
            self.assertEqual(pending['finished_at'],cancelled_at)
            self.assertIsNone(pending['started_at'])
            self.assertEqual(pending['elapsed_seconds'],0)
            wall[0]+=3600;steady[0]+=3600
            self.backend.entered.clear();self.backend.release.clear()
            self.service.dispatch('retry','tasks.retry',{'ids':[first['id']]},None)
            self.assertTrue(self.backend.entered.wait(1))
            self.assertIsNone(first['finished_at'])
            self.assertEqual(first['started_at'],created)
            wall[0]-=120;steady[0]+=6  # Clock adjustment must not corrupt duration.
            self.backend.release.set();self.service.transfers.submit(lambda:None).result(timeout=3)
            self.assertEqual((first['state'],first['elapsed_seconds']),('done',10))
            self.assertEqual(first['finished_at'],int(wall[0]*1000))
            self.assertEqual(first['created_at'],created)
        loaded=TaskStore(self.service.task_store.path).load()[0]
        self.assertEqual({key:loaded[key] for key in TIMING_FIELDS},{key:first[key] for key in TIMING_FIELDS})
        self.assertNotIn('_run_started',self.service.task_store.path.read_text('utf-8'))

    def test_legacy_records_load_without_inventing_dates_and_migrate_on_save(self):
        with patch.object(self.service,'start_tasks'):self.queue('legacy')
        self.service.tasks[0]['state']='done';self.service.persist_tasks()
        path=self.service.task_store.path
        document=json.loads(path.read_text('utf-8'))
        document['version']=1
        for row in document['tasks']:
            for key in TIMING_FIELDS:row.pop(key)
        path.write_text(json.dumps(document),encoding='utf-8')
        store=TaskStore(path);rows=store.load()
        self.assertTrue(store.writable)
        self.assertTrue(all(rows[0][key] is None for key in TIMING_FIELDS))
        self.assertTrue(store.save(rows))
        saved=json.loads(path.read_text('utf-8'))
        self.assertEqual(saved['version'],2)
        self.assertEqual(saved['tasks'][0]['id'],document['tasks'][0]['id'])
        self.assertTrue(all(saved['tasks'][0][key] is None for key in TIMING_FIELDS))

    def test_invalid_record_times_preserve_the_original_store(self):
        with patch.object(self.service,'start_tasks'):self.queue('invalid')
        self.service.persist_tasks();path=self.service.task_store.path
        document=json.loads(path.read_text('utf-8'))
        for value in (-1,True,'yesterday',float('nan'),float('inf')):
            with self.subTest(value=value):
                document['tasks'][0]['finished_at']=value
                original=json.dumps(document);path.write_text(original,encoding='utf-8')
                store=TaskStore(path)
                self.assertEqual(store.load(),[])
                self.assertFalse(store.writable)
                self.assertFalse(store.save([]))
                self.assertEqual(path.read_text('utf-8'),original)

    def test_clear_ids_removes_only_selected_finished_records_and_persists(self):
        with patch.object(self.service,'start_tasks'):
            for fid in ('selected','other','failed','cancelled'):
                self.queue(fid)
        for task,state in zip(self.service.tasks,('done','done','failed','cancelled')):
            task['state']=state
        selected,other,failed,cancelled=[task['id'] for task in self.service.tasks]
        result=self.service.dispatch('clear','tasks.clear',{'ids':[selected,failed]},None)
        self.assertEqual([task['id'] for task in result],[other,failed,cancelled])
        stored=json.loads(self.service.task_store.path.read_text('utf8'))
        self.assertEqual([task['id'] for task in stored['tasks']],[other,failed,cancelled])
        result=self.service.dispatch('empty','tasks.clear',{'ids':[]},None)
        self.assertEqual([task['id'] for task in result],[other,failed,cancelled])
        result=self.service.dispatch('all','tasks.clear',{},None)
        self.assertEqual([task['id'] for task in result],[failed])

    def test_progress_pauses_without_stale_rate_and_retry_discards_old_snapshot(self):
        def download(request,progress,stopped):
            progress(ProgressEvent('download','transferring','当前.txt',80,64,100,20,connections=4))
            self.backend.entered.set()
            self.backend.release.wait(3)
            if stopped():
                raise DownloadStopped('已暂停')
            return {'size':100}
        self.backend.download=download
        self.queue('progress')
        self.assertTrue(self.backend.entered.wait(1))
        snapshot=self.service.dispatch('snapshot','tasks',{},None)[0]['progress']
        self.assertEqual(snapshot,{'name':'当前.txt','phase':'transferring','transferred':80,'confirmed':64,
            'total':100,'rate':20,'connections':4,'recursive':False})
        self.service.stop.set();self.backend.release.set();self.wait_idle()
        paused=self.service.dispatch('paused','tasks',{},None)[0]
        self.assertEqual((paused['state'],paused['progress']['rate'],paused['progress']['confirmed']),('stopped',0,64))
        document=json.loads(self.service.task_store.path.read_text('utf8'))
        self.assertNotIn('progress',document['tasks'][0])
        gate=threading.Event();self.addCleanup(gate.set)
        self.service.transfers.submit(lambda:gate.wait(3))
        waiting=self.service.dispatch('retry','tasks.retry',{'ids':[paused['id']]},None)[0]
        self.assertEqual(waiting['state'],'waiting');self.assertIsNone(waiting['progress'])
        gate.set();self.wait_idle()
        finished=self.service.dispatch('done','tasks',{},None)[0]
        self.assertEqual((finished['state'],finished['progress']['transferred'],finished['progress']['total']),('done',100,100))
        self.assertEqual(finished['progress']['rate'],0)

    def test_directory_progress_uses_existing_aggregate_and_relative_current_file(self):
        def download(request,progress,stopped):
            RecursiveTransfer._progress(progress,'子目录/当前.bin',20,100)(
                ProgressEvent('download','transferring','当前.bin',50,30,60,10,connections=8))
            return {'recursive':True,'size':100,'files':2,'directories':1,'path':'目录'}
        self.backend.download=download
        self.service.dispatch('directory','download',{'entries':[{'fid':'folder','name':'目录','directory':True}]},None)
        self.wait_idle()
        reports=[message['data']['progress'] for message in self.messages if message.get('event') == 'task' and message['data'].get('progress')]
        self.assertEqual((reports[0]['recursive'],reports[0]['name'],reports[0]['transferred'],reports[0]['confirmed'],reports[0]['total']),
            (True,'子目录/当前.bin',70,50,100))
        self.assertEqual((reports[-1]['confirmed'],reports[-1]['total']),(100,100))

    def test_unknown_share_phases_and_empty_file_have_no_invented_total(self):
        def download(request,progress,stopped):
            for phase in ('saving','cleaning'):
                progress(ProgressEvent('download',phase,'当前.txt',0,0,None,0))
            return {'size':0}
        self.backend.download=download;self.queue('empty');self.wait_idle()
        reports=[message['data']['progress'] for message in self.messages if message.get('event') == 'task' and message['data'].get('progress')]
        self.assertEqual([report['phase'] for report in reports],['saving','cleaning','completed'])
        self.assertIsNone(reports[0]['total']);self.assertIsNone(reports[1]['total'])
        self.assertEqual((reports[-1]['total'],reports[-1]['transferred']),(0,0))

    def test_upload_progress_and_verified_file_size_use_the_same_public_snapshot(self):
        source=Path(self.tmp.name)/'上传.bin';source.write_bytes(b'0123456789')
        def upload(source,parent,**options):
            options['progress'](ProgressEvent('upload','transferring','上传.bin',5,0,10,2))
            return {'file_size':10}
        with patch('quark_cli.desktop.service.Transfer.upload',side_effect=upload):
            self.service.dispatch('upload','upload',{'sources':[str(source)],'parent':'0'},None)
            self.wait_idle()
        reports=[message['data'] for message in self.messages if message.get('event') == 'task' and message['data'].get('progress')]
        self.assertEqual((reports[0]['direction'],reports[0]['progress']['transferred']),('upload',5))
        self.assertEqual((reports[-1]['state'],reports[-1]['progress']['confirmed'],reports[-1]['progress']['total']),('done',10,10))
        self.assertLessEqual(reports[-1]['created_at'],reports[-1]['started_at'])
        self.assertLessEqual(reports[-1]['started_at'],reports[-1]['finished_at'])
        self.assertGreaterEqual(reports[-1]['elapsed_seconds'],0)

    def test_append_during_download_remains_sequential_and_durable(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        self.queue("b")
        self.assertEqual(self.service.pending_count, 2)
        self.backend.release.set()
        self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests], ["a", "b"])
        self.assertEqual(self.backend.maximum, 1)
        document = json.loads(self.service.task_store.path.read_text("utf-8"))
        self.assertEqual([row["state"] for row in document["tasks"]], ["done", "done"])
        self.assertNotIn("sid=queue-credential", str(document))
        started = [message for message in self.messages if message.get('event') == 'batch.started']
        finished = [message for message in self.messages if message.get('event') == 'batch.finished']
        self.assertEqual(len(started), 1)
        self.assertEqual(len(finished), 1)
        self.assertEqual(started[0]['data']['batch_id'], finished[0]['data']['batch_id'])
        self.assertEqual(finished[0]['data']['counts'], {'done':2,'failed':0,'stopped':0,'cancelled':0})
        self.queue('c')
        self.wait_idle()
        later = [message for message in self.messages if message.get('event') == 'batch.finished'][-1]['data']
        self.assertNotEqual(later['batch_id'], finished[0]['data']['batch_id'])
        self.assertEqual(later['counts']['done'], 1)

    def test_individual_pause_preserves_queue_and_can_restart(self):
        self.queue('a');self.assertTrue(self.backend.entered.wait(1));self.queue('b')
        first=self.service.tasks[0]['id']
        paused=self.service.dispatch('pause','tasks.pause',{'ids':[first]},None)
        self.assertTrue(paused[0]['pausing'])
        self.assertFalse(self.service.stop.is_set())
        self.backend.release.set();self.wait_idle()
        self.assertEqual([row['state'] for row in self.service.tasks],['stopped','done'])
        self.service.dispatch('start','tasks.retry',{'ids':[first]},None);self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests],['a','b','a'])
        self.assertEqual(self.backend.maximum,1)

    def test_resume_paused_pending_task_never_submits_duplicate_future(self):
        self.queue('a');self.assertTrue(self.backend.entered.wait(1));self.queue('b')
        second=self.service.tasks[1]['id']
        self.service.dispatch('pause','tasks.pause',{'ids':[second]},None)
        self.assertEqual(self.service.tasks[1]['state'],'stopped')
        self.service.dispatch('start','tasks.retry',{'ids':[second]},None)
        self.assertEqual(self.service.pending_count,2)
        self.backend.release.set();self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests],['a','b'])
        self.assertEqual(self.backend.maximum,1)

    def test_restarting_paused_task_during_other_transfer_counts_each_record_once(self):
        second_entered=threading.Event();second_release=threading.Event()
        self.addCleanup(second_release.set)
        original=self.backend.download
        def download(request,progress,stopped):
            if request.fid == 'b':
                second_entered.set();second_release.wait(3)
            return original(request,progress,stopped)
        self.backend.download=download
        self.queue('a');self.assertTrue(self.backend.entered.wait(1));self.queue('b')
        first=self.service.tasks[0]['id']
        self.service.dispatch('pause','tasks.pause',{'ids':[first]},None)
        self.backend.release.set();self.assertTrue(second_entered.wait(1))
        self.service.dispatch('restart','tasks.retry',{'ids':[first]},None)
        second_release.set();self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests],['a','b','a'])
        finished=[message for message in self.messages if message.get('event') == 'batch.finished']
        self.assertEqual(len(finished),1)
        self.assertEqual(finished[0]['data']['counts']['done'],2)

    def test_remove_running_and_pending_records_waits_for_safe_stop_and_retains_files(self):
        self.queue('a');self.assertTrue(self.backend.entered.wait(1));self.queue('b');self.queue('c')
        first,second,third=[task['id'] for task in self.service.tasks]
        local=Path(self.tmp.name)/'a.txt';local.write_text('retain me',encoding='utf8')
        before=len(self.messages)
        result=self.service.dispatch('remove','tasks.remove',{'ids':[first,second]},None)
        self.assertEqual([task['id'] for task in result],[first,third])
        self.assertTrue(result[0]['removing']);self.assertTrue(self.service.busy)
        self.backend.release.set();self.wait_idle()
        self.assertEqual([task['id'] for task in self.service.tasks],[third])
        self.assertEqual([request.fid for request in self.backend.requests],['a','c'])
        self.assertEqual(local.read_text('utf8'),'retain me')
        self.assertFalse(any(message.get('event') == 'task' and message['data']['id'] in (first,second) for message in self.messages[before:]))
        stored=json.loads(self.service.task_store.path.read_text('utf8'))
        self.assertEqual([task['id'] for task in stored['tasks']],[third])
        self.service.dispatch('remove-done','tasks.remove',{'ids':[third]},None)
        self.assertEqual(self.service.tasks,[])
        self.assertEqual(local.read_text('utf8'),'retain me')

    def test_empty_retry_ids_does_not_restart_other_tasks(self):
        self.queue('a');self.assertTrue(self.backend.entered.wait(1))
        self.service.dispatch('pause','tasks.pause',{'ids':[self.service.tasks[0]['id']]},None)
        self.backend.release.set();self.wait_idle()
        from quark_cli.errors import UsageError
        with self.assertRaises(UsageError):
            self.service.dispatch('empty','tasks.retry',{'ids':[]},None)
        self.assertEqual(len(self.backend.requests),1)

    def test_remove_response_precedes_final_cleanup_event(self):
        self.queue('a');self.assertTrue(self.backend.entered.wait(1))
        identifier=self.service.tasks[0]['id']
        dispatch=self.service.dispatch
        def delayed_dispatch(*args):
            result=dispatch(*args)
            self.backend.release.set()
            threading.Event().wait(0.04)
            return result
        with patch.object(self.service,'dispatch',side_effect=delayed_dispatch):
            self.service.execute('remove','tasks.remove',{'ids':[identifier]})
        self.wait_idle()
        response=next(index for index,item in enumerate(self.messages) if item.get('id') == 'remove')
        final=next(index for index,item in enumerate(self.messages) if item.get('event') == 'tasks.changed' and item['data'] == [])
        self.assertLess(response,final)


    def test_mixed_upload_preflight_reads_target_listing_once_for_multiple_folders(self):
        from quark_cli.downloader.worker import DriveBackend
        from tests.test_desktop_files import Drive
        from tests.test_downloader_worker import COOKIE
        drive=Drive([])
        backend=DriveBackend(lambda:COOKIE,transport=drive.transport())
        service=DesktopService(self.messages.append,backend=backend,directory=Path(self.tmp.name)/'upload-state')
        self.addCleanup(service.shutdown)
        service.saved_cookie=COOKIE
        sources=[Path(self.tmp.name)/name for name in ('资料一','资料二','文件.txt')]
        sources[0].mkdir();sources[1].mkdir();sources[2].write_text('payload',encoding='utf8')
        with patch.object(service,'start_tasks'), patch('quark_cli.recursive.config_directory',return_value=Path(self.tmp.name)/'checkpoints'):
            result=service.dispatch('upload','upload',{'sources':[str(source) for source in sources],'parent':'0'},None)
        self.assertEqual(len(result),3)
        self.assertEqual([task['request'].recursive for task in service.tasks],[True,True,False])
        listings=[request for request in drive.requests if request.url.path.endswith('file/sort')]
        self.assertEqual(len(listings),1)
        self.assertEqual(listings[0].url.params['pdir_fid'],'0')
        self.assertEqual(drive.writes,[])

    def test_shutdown_waits_for_active_cleanup_and_persists_queued_pause(self):
        self.queue('active')
        self.assertTrue(self.backend.entered.wait(1))
        self.queue('queued')
        finished = threading.Event()
        def shutdown():
            self.service.shutdown()
            finished.set()
        closing = threading.Thread(target=shutdown)
        closing.start()
        try:
            self.assertTrue(self.service.stop.wait(1))
            self.assertFalse(finished.is_set())
            self.service.accept({'id':'closing-request','method':'download','params':{'entries':[]}})
            self.assertIn('正在退出', self.messages[-1]['error'])
            with self.assertRaises(Exception):
                self.service.start_tasks([])
            self.assertTrue(self.service.stop.is_set())
        finally:
            self.backend.release.set()
            closing.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual([request.fid for request in self.backend.requests], ['active'])
        self.assertEqual([task['state'] for task in self.service.tasks], ['stopped','stopped'])
        self.assertFalse(self.service.busy)
        document=json.loads(self.service.task_store.path.read_text('utf8'))
        self.assertEqual([task['state'] for task in document['tasks']], ['stopped','stopped'])
        summary=[message for message in self.messages if message.get('event') == 'batch.finished'][-1]['data']
        self.assertTrue(summary['stopped'])
        self.assertEqual(summary['counts']['stopped'], 2)

    def test_batch_summary_counts_failed_and_cancelled_without_old_results(self):
        original=self.backend.download
        def download(request, progress, stopped):
            if request.fid == 'fail':
                raise RuntimeError('controlled failure')
            return original(request, progress, stopped)
        self.backend.download=download
        self.queue('done')
        self.assertTrue(self.backend.entered.wait(1))
        self.queue('fail')
        queued=self.queue('cancelled')[-1]
        self.service.dispatch('cancel','tasks.cancel',{'ids':[queued['id']]},None)
        self.backend.release.set()
        self.wait_idle()
        summary=[message for message in self.messages if message.get('event') == 'batch.finished'][-1]['data']
        self.assertEqual(summary['counts'], {'done':1,'failed':1,'stopped':0,'cancelled':1})

    def test_cancel_waiting_item_never_calls_backend(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        tasks = self.queue("b")
        self.service.dispatch("cancel", "tasks.cancel", {"ids": [tasks[1]["id"]]}, None)
        self.backend.release.set()
        self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests], ["a"])
        self.assertEqual(self.service.tasks[1]["state"], "cancelled")

    def test_pause_and_restart_do_not_autorun_and_other_cookie_cannot_retry(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        self.service.stop.set()
        self.backend.release.set()
        self.wait_idle()
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=other-credential"
        tasks = other.dispatch("tasks", "tasks", {}, None)
        self.assertEqual(tasks[0]["state"], "stopped")
        self.assertFalse(tasks[0]["valid"])
        with self.assertRaises(Exception):
            other.dispatch("retry", "tasks.retry", {}, None)
        self.assertEqual(other.backend.requests, [])

    def test_share_record_has_no_code_and_restart_requires_authorization(self):
        source = self.service.dispatch(
            "share",
            "share.open",
            {"text": "https://pan.quark.cn/s/abcdef?pwd=1234"},
            None,
        )
        self.queue("share-file", handle=source["handle"])
        self.assertTrue(self.backend.entered.wait(1))
        self.service.stop.set()
        self.backend.release.set()
        self.wait_idle()
        document = self.service.task_store.path.read_text("utf-8")
        self.assertNotIn("1234", document)
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=queue-credential"
        task = other.dispatch("tasks", "tasks", {}, None)[0]
        self.assertTrue(task["needs_authorization"])
        self.assertFalse(task["valid"])

    def test_running_record_restores_as_paused(
        self,
    ):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        self.assertEqual(other.tasks[0]["state"], "stopped")
        self.assertIsNotNone(other.tasks[0]['created_at'])
        self.assertIsNotNone(other.tasks[0]['started_at'])
        self.assertIsNone(other.tasks[0]['finished_at'])
        self.assertIsNone(other.tasks[0]['elapsed_seconds'])
        self.assertEqual(other.backend.requests, [])

    def test_corrupt_record_keeps_session_queue_without_overwriting_original(self):
        self.service.directory.mkdir(parents=True, exist_ok=True)
        path = self.service.task_store.path
        path.write_text("{corrupt", encoding="utf-8")
        other = DesktopService(
            self.messages.append, backend=Backend(), directory=self.service.directory
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=queue-credential"
        other.dispatch(
            "download",
            "download",
            {"entries": [{"fid": "x", "name": "x.txt", "directory": False}]},
            None,
        )
        other.transfers.shutdown(wait=True)
        self.assertEqual(path.read_text("utf-8"), "{corrupt")
        self.assertEqual(other.tasks[0]["state"], "done")
        self.assertTrue(other.task_store.warning)
