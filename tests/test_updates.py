import hashlib
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state
import ayugram
import updates


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mac = self.root/'_apps/macOS'
        self.mac.mkdir(parents=True)
        self.tg = self.bundle(self.mac/'Telegram.app', 'Telegram', '7.3.1')
        self.ayu = self.bundle(self.mac/'AyuGram.app', 'AyuGram', '7.2.9')
        self.manifest = self.mac/'.ayugram-official.json'
        self.manifest.write_text(json.dumps({'release':'7.2.9', 'fingerprint':ayugram.bundle_fingerprint(str(self.ayu))}))
        self.account = self.root/'account/TelegramForcePortable/tdata'
        self.account.mkdir(parents=True)
        (self.account/'key_datas').write_bytes(b'original session unchanged')
        for obj, key, value in [(state,'DATA_DIR',str(self.root)),(server,'DATA_DIR',str(self.root)),
                                (server,'SHARED_APPS_DIR',str(self.root/'_apps')),
                                (server,'SHARED_MACOS_APP',str(self.tg))]:
            patch=mock.patch.object(obj,key,value);patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(server,'get_shared_app',return_value=str(self.tg));patch.start();self.addCleanup(patch.stop)

    def bundle(self, path, client, version):
        (path/'Contents/MacOS').mkdir(parents=True)
        (path/'Contents/MacOS'/client).write_bytes((client+' '+version).encode())
        with (path/'Contents/Info.plist').open('wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'com.tdesktop.Telegram' if client=='Telegram' else ayugram.BUNDLE_ID,
                          'CFBundleShortVersionString':version},f)
        return path

    def release(self,client='AyuGram',version='7.3.1'):
        repo=updates.REPOSITORIES[client]
        name='AyuGram.dmg' if client=='AyuGram' else 'td-setup-mac-'+version+'.dmg'
        return {'client':client,'version':version,'tag':'v'+version,'sha256':'a'*64,
                'download_url':f'https://github.com/{repo}/releases/download/v{version}/{name}',
                'release_url':f'https://github.com/{repo}/releases/tag/v{version}'}

    def metadata(self,client='AyuGram',version='7.3.1'):
        release=self.release(client,version)
        return {'tag_name':release['tag'],'assets':[{'name':release['download_url'].split('/')[-1],
                'browser_download_url':release['download_url'],'digest':'sha256:'+release['sha256']}]}

    def response(self,payload,url='https://release-assets.githubusercontent.com/file'):
        response=io.BytesIO(payload)
        response.geturl=lambda:url
        return response

    def test_official_metadata_selects_verified_macos_asset(self):
        for client in updates.REPOSITORIES:
            with mock.patch.object(updates.urllib.request,'urlopen',return_value=self.response(json.dumps(self.metadata(client)).encode())):
                result=updates.official_release(client)
            self.assertEqual(result['sha256'],'a'*64)
            self.assertEqual(result['client'],client)

    def test_metadata_rejects_untrusted_url_missing_hash_and_prerelease(self):
        for change in ['url','hash','prerelease','duplicate']:
            data=self.metadata()
            if change=='url':data['assets'][0]['browser_download_url']='https://example.com/evil.dmg'
            if change=='hash':data['assets'][0]['digest']=None
            if change=='prerelease':data['prerelease']=True
            if change=='duplicate':data['assets']*=2
            with self.subTest(change=change), mock.patch.object(updates.urllib.request,'urlopen',return_value=self.response(json.dumps(data).encode())):
                with self.assertRaises(ValueError):updates.official_release('AyuGram')

    def test_api_rate_limit_uses_official_page_and_exact_asset_checksum(self):
        repo=updates.REPOSITORIES['AyuGram']
        assets=(f'<li><a href="/{repo}/releases/download/v7.3.1/AyuGram.zip">Windows</a><clipboard-copy value="sha256:{"b"*64}"></clipboard-copy></li>'
                f'<li><a href="/{repo}/releases/download/v7.3.1/AyuGram.dmg">Mac</a><clipboard-copy value="sha256:{"a"*64}"></clipboard-copy></li>').encode()
        error=updates.urllib.error.HTTPError('https://api.github.com',403,'rate limited',{},None)
        with mock.patch.object(updates.urllib.request,'urlopen',side_effect=[error,self.response(b'',f'https://github.com/{repo}/releases/tag/v7.3.1'),self.response(assets)]):
            release=updates.official_release('AyuGram')
        self.assertEqual(release['sha256'],'a'*64)
        self.assertTrue(release['download_url'].endswith('AyuGram.dmg'))

    def test_online_failure_keeps_installed_versions_and_disables_install(self):
        with mock.patch.object(updates,'official_release',side_effect=OSError('offline')):
            result=updates.check_releases()
        self.assertEqual([r['installed_version'] for r in result['clients']],['7.3.1','7.2.9'])
        self.assertTrue(all('error' in r and not r['update_available'] for r in result['clients']))
        self.assertIn('newer',result['warning'])

    def test_download_verifies_digest_and_redirect(self):
        payload=b'official fixture'
        release=self.release();release['sha256']=hashlib.sha256(payload).hexdigest()
        with mock.patch.object(updates.urllib.request,'urlopen',return_value=self.response(payload)):
            updates._download(release,str(self.root/'good.dmg'))
        for url,body in [('https://example.com/file',payload),('https://github.com/file',b'corrupt')]:
            with mock.patch.object(updates.urllib.request,'urlopen',return_value=self.response(body,url)):
                with self.assertRaises(ValueError):updates._download(release,str(self.root/'bad.dmg'))

    def test_install_rechecks_warning_before_any_download(self):
        release=self.release('Telegram','7.3.2')
        with mock.patch.object(updates,'official_release',return_value=release),mock.patch.object(updates,'_download') as download:
            result=updates.install_release('Telegram','7.3.2')
        self.assertTrue(result['confirmation_required'])
        self.assertEqual(result['acknowledgement'],{'Telegram':'7.3.2','AyuGram':'7.2.9'})
        download.assert_not_called()

    def test_changed_release_same_version_and_downgrade_are_refused(self):
        for candidate,requested in [('7.3.2','7.3.1'),('7.2.9','7.2.9'),('7.0.9','7.0.9')]:
            with mock.patch.object(updates,'official_release',return_value=self.release(version=candidate)),mock.patch.object(updates,'_download') as download:
                result=updates.install_release('AyuGram',requested)
                self.assertFalse(result['success'])
                download.assert_not_called()

    def test_unknown_process_state_and_running_clients_prevent_install(self):
        with mock.patch.object(server,'_get_ps_output',return_value=None):
            with self.assertRaises(ValueError):updates._require_closed_accounts()
        with mock.patch.object(server,'_get_ps_output',return_value='123 running'),mock.patch.object(server,'scan_accounts_cached',return_value=[{'path':'account'}]),mock.patch.object(server,'_telegram_pid_from_ps_output',return_value=123):
            with self.assertRaises(ValueError):updates._require_closed_accounts()

    def install_fixture(self,client='AyuGram',fail_manifest=False,detach_failure=False):
        version='7.3.1' if client=='AyuGram' else '7.3.2'
        release=self.release(client,version)
        def run(command,**kwargs):
            if command[:2]==['hdiutil','attach']:
                mount=Path(command[command.index('-mountpoint')+1])
                self.bundle(mount/(client+'.app'),client,version)
            return mock.Mock(returncode=1 if detach_failure and command[:2]==['hdiutil','detach'] else 0,stderr=b'')
        def copy(source,dest,*args,**kwargs):
            if os.path.exists(dest):shutil.rmtree(dest)
            shutil.copytree(source,dest)
            return True,''
        saved=state._save_json_atomic
        failed=False
        def save(path,value):
            nonlocal failed
            if fail_manifest and not failed and path==str(self.manifest):
                failed=True
                raise OSError('disk full')
            saved(path,value)
        with (mock.patch.object(updates,'official_release',return_value=release),mock.patch.object(updates,'_download',side_effect=lambda release,path:Path(path).write_bytes(b'fixture')),
             mock.patch.object(updates,'_require_closed_accounts'),mock.patch.object(server,'_copy_app_bundle',side_effect=copy),mock.patch.object(server,'_verify_tdesktop_bundle',return_value=(True,'')),
             mock.patch.object(updates.subprocess,'run',side_effect=run),mock.patch.object(state,'_save_json_atomic',side_effect=save)):
            return updates.install_release(client,version,{'Telegram':version,'AyuGram':'7.2.9'} if client=='Telegram' else None)

    def test_ayugram_install_preserves_previous_app_manifest_and_session(self):
        result=self.install_fixture()
        self.assertTrue(result['success'],result)
        self.assertEqual(server._bundle_version(str(self.ayu)),'7.3.1')
        self.assertEqual(server._bundle_version(str(self.mac/'AyuGram.app.previous')),'7.2.9')
        self.assertEqual(json.loads(self.manifest.read_text())['fingerprint'],ayugram.bundle_fingerprint(str(self.ayu)))
        self.assertEqual((self.account/'key_datas').read_bytes(),b'original session unchanged')
        self.assertFalse((self.mac/'.ayugram-update.json').exists())

    def test_telegram_install_preserves_previous_app_and_session(self):
        result=self.install_fixture('Telegram')
        self.assertTrue(result['success'],result)
        self.assertEqual(server._bundle_version(str(self.tg)),'7.3.2')
        self.assertEqual(server._bundle_version(str(self.mac/'Telegram.app.previous')),'7.3.1')
        self.assertEqual((self.account/'key_datas').read_bytes(),b'original session unchanged')

    def test_failed_manifest_commit_rolls_back_app_and_trust_record(self):
        original=self.manifest.read_bytes()
        result=self.install_fixture(fail_manifest=True)
        self.assertFalse(result['success'])
        self.assertEqual(server._bundle_version(str(self.ayu)),'7.2.9')
        self.assertEqual(json.loads(self.manifest.read_text()),json.loads(original))
        self.assertEqual((self.account/'key_datas').read_bytes(),b'original session unchanged')

    def test_cleanup_failure_reports_that_installation_completed(self):
        result=self.install_fixture(detach_failure=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['installed'])
        self.assertIn('was installed',result['message'])
        self.assertEqual(server._bundle_version(str(self.ayu)),'7.3.1')

    def test_recovery_restores_interrupted_installation(self):
        old=json.loads(self.manifest.read_text())
        self.ayu.rename(self.mac/'AyuGram.app.previous')
        self.bundle(self.ayu,'AyuGram','7.3.1')
        (self.mac/'.ayugram-update.json').write_text(json.dumps({'had_master':True,'manifest':old}))
        updates.recover_ayugram_update()
        self.assertEqual(server._bundle_version(str(self.ayu)),'7.2.9')
        self.assertEqual(json.loads(self.manifest.read_text()),old)

    def test_shared_directory_symlink_is_rejected(self):
        shutil.rmtree(self.root/'_apps')
        outside=self.root/'outside';outside.mkdir()
        (self.root/'_apps').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):updates._shared_directory()

    def test_manifest_staging_hardlink_cannot_modify_session_files(self):
        key=self.account/'key_datas'
        os.link(key,str(self.manifest)+'.tmp')
        with self.assertRaises(ValueError):updates._ayu_paths()
        self.assertEqual(key.read_bytes(),b'original session unchanged')

    def test_download_size_limit_stops_before_installation(self):
        with mock.patch.object(updates,'MAX_DOWNLOAD',4),mock.patch.object(updates.urllib.request,'urlopen',return_value=self.response(b'oversized')):
            with self.assertRaises(ValueError):updates._download(self.release(),str(self.root/'large.dmg'))
        self.assertEqual(server._bundle_version(str(self.ayu)),'7.2.9')

    def test_launch_and_second_install_are_blocked_during_installation(self):
        import threading
        entered=threading.Event();release=threading.Event()
        def hold():
            with updates.INSTALL_LOCK:
                entered.set();release.wait(5)
        thread=threading.Thread(target=hold);thread.start();entered.wait(5)
        try:
            self.assertFalse(server.open_account(str(self.account.parent.parent))[0])
            self.assertFalse(ayugram.open_account(str(self.account.parent.parent))[0])
            self.assertFalse(updates.install_release('AyuGram','7.3.1')['success'])
            self.assertEqual((self.account/'key_datas').read_bytes(),b'original session unchanged')
        finally:
            release.set();thread.join(5)

    def test_invalid_recovery_record_cannot_remove_existing_apps(self):
        previous=self.bundle(self.mac/'AyuGram.app.previous','AyuGram','7.0.9')
        before=ayugram.bundle_fingerprint(str(self.ayu))
        (self.mac/'.ayugram-update.json').write_text(json.dumps({'had_master':True,'manifest':'invalid'}))
        with self.assertRaises(ValueError):updates.recover_ayugram_update()
        self.assertEqual(ayugram.bundle_fingerprint(str(self.ayu)),before)
        self.assertTrue(previous.exists())

    def test_new_routes_dispatch_to_controlled_updater(self):
        responses=[]
        class Handler:
            def send_json(self,body):responses.append(body)
        with mock.patch.object(updates,'check_releases',return_value={'success':True}) as check:
            server.RequestHandler._post_api_check_client_updates(Handler(),{})
            check.assert_called_once()
        with mock.patch.object(updates,'install_release',return_value={'success':False}) as install:
            server.RequestHandler._post_api_install_client_update(Handler(),{'client':'AyuGram','version':'7.3.1','acknowledgement':{'Telegram':'7.3.1'}})
            install.assert_called_once_with('AyuGram','7.3.1',{'Telegram':'7.3.1'})
        self.assertIn('/api/install-client-update',server.RequestHandler.POST_ROUTES)

    def test_frontend_confirmation_cancel_and_refresh_are_correct(self):
        import subprocess
        source=Path(__file__).resolve().parents[1]/'TelegramManager.app/Contents/Resources/index.html'
        script=r'''
const text=require('fs').readFileSync(process.argv[1],'utf8');
const nodes={};const calls=[];
const document={getElementById:id=>nodes[id] ||= {style:{},classList:{add(){},remove(){}},textContent:'',innerHTML:''},querySelectorAll:()=>[]};
const esc=value=>String(value);
const toastResult=()=>{};const loadSharedAppStatus=()=>{};const refresh=async()=>{};
const ack={Telegram:'7.4.0',AyuGram:'7.2.9'};
const api=async(route,method,body)=>{
 calls.push([route,body]);
 if(route==='/api/check-client-updates')return {success:true,clients:[{client:'Telegram',installed_version:'7.3.1',version:'7.4.0',update_available:true},{client:'AyuGram',installed_version:'7.2.9',version:'7.2.9',update_available:false}],warning:'Version mismatch'};
 if(!body.acknowledgement)return {success:false,confirmation_required:true,warning:'Confirm version mismatch',acknowledgement:ack};
 return {success:true,message:'Installed'};
};
eval(text.slice(text.indexOf('let clientUpdateBusy ='),text.indexOf('let pendingTelegramUpdate =')));
(async()=>{
 await showClientUpdates();
 await installClientUpdate('Telegram');
 if(nodes['client-update-confirm'].style.display!=='')throw Error('Missing warning confirmation');
 hideClientUpdates();
 const before=calls.length;await confirmClientUpdate();
 if(calls.length!==before)throw Error('Installed after cancellation');
 await showClientUpdates();await installClientUpdate('Telegram');await confirmClientUpdate();
 if(JSON.stringify(calls.at(-1)[1].acknowledgement)!==JSON.stringify(ack))throw Error('Wrong acknowledgement');
 if(!nodes['client-update-list'].innerHTML.includes('Installed: 7.4.0'))throw Error('Stale installed version');
 if(!nodes['client-update-list'].innerHTML.includes('Up to date'))throw Error('Stale update availability');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_app_interface_has_no_remaining_romanian_labels(self):
        resources=Path(server.__file__).parent
        for name in ('index.html','server.py','ayugram.py','updates.py','launcher.swift'):
            self.assertNotRegex((resources/name).read_text(),r'[ăâîșțĂÂÎȘȚ]',name)

    def inline_bundle(self, path):
        app = self.bundle(path, 'AyuGram', '7.2.9')
        (app/'Contents/MacOS/AyuGram').rename(app/'Contents/MacOS'/ayugram.INLINE_EXECUTABLE)
        info = app/'Contents/Info.plist'
        payload = plistlib.loads(info.read_bytes())
        payload.update(CFBundleIdentifier=ayugram.INLINE_BUNDLE_ID,
                       CFBundleExecutable=ayugram.INLINE_EXECUTABLE)
        info.write_bytes(plistlib.dumps(payload))
        return app

    def test_local_inline_install_pins_provenance_and_preserves_previous_app(self):
        source = self.inline_bundle(self.root/'inline.app')
        fingerprint = ayugram.bundle_fingerprint(str(source))
        release = {'kind':'local-inline','version':'7.2.9','fingerprint':fingerprint}
        def copy(src, dst):
            shutil.copytree(src, dst)
            return True, ''
        with mock.patch.object(server,'_copy_app_bundle',side_effect=copy), \
             mock.patch.object(updates.subprocess,'run',return_value=mock.Mock(returncode=0)) as run:
            updates._replace_ayugram(str(source), release)
        self.assertEqual(ayugram._trusted_master(str(self.ayu)), (fingerprint, True))
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest['kind'], 'local-inline')
        self.assertNotIn('sha256', manifest)  # No claim of an official package checksum.
        self.assertTrue((self.mac/'AyuGram.app.previous/Contents/MacOS/AyuGram').is_file())
        self.assertEqual((self.account/'key_datas').read_bytes(), b'original session unchanged')
        self.assertEqual(ayugram.bundle_fingerprint(str(source)), fingerprint)
        run.assert_called_once_with(['codesign','--verify','--deep','--strict',str(source)],
                                    capture_output=True, timeout=120)

    def test_local_inline_install_rejects_wrong_pin_and_signature_before_copy(self):
        source = self.inline_bundle(self.root/'inline.app')
        fingerprint = ayugram.bundle_fingerprint(str(source))
        for pin, code in [('wrong', 0), (fingerprint, 1)]:
            with self.subTest(pin=pin, code=code), \
                 mock.patch.object(server,'_copy_app_bundle') as copy, \
                 mock.patch.object(updates.subprocess,'run',return_value=mock.Mock(returncode=code)):
                with self.assertRaises(ValueError):
                    updates._replace_ayugram(str(source), {'kind':'local-inline','version':'7.2.9','fingerprint':pin})
                copy.assert_not_called()
        self.assertEqual((self.account/'key_datas').read_bytes(), b'original session unchanged')

    def test_official_update_warns_before_replacing_inline_build_even_when_versions_match(self):
        shutil.rmtree(self.ayu)
        self.inline_bundle(self.ayu)
        with mock.patch.object(updates,'official_release',side_effect=lambda client:self.release(client)), \
             mock.patch.object(updates,'_download') as download:
            rows = updates.check_releases()['clients']
            self.assertIn('persistent inline', rows[1]['installed_variant'])
            result = updates.install_release('AyuGram','7.3.1')
            self.assertTrue(result['confirmation_required'])
            self.assertIn('replace the custom',result['warning'])
            self.assertTrue(result['acknowledgement']['replaces_inline_history'])
            stale = {'Telegram':'7.3.1','AyuGram':'7.3.1'}
            self.assertTrue(updates.install_release('AyuGram','7.3.1',stale)['confirmation_required'])
            download.assert_not_called()
            with mock.patch.object(updates,'_require_closed_accounts',side_effect=ValueError('synthetic stop')) as closed:
                acknowledged = updates.install_release('AyuGram','7.3.1',result['acknowledgement'])
                closed.assert_called_once()
                self.assertFalse(acknowledged.get('confirmation_required'))
