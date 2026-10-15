import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN','unit-test-token')
import server
import updates

class WeeklyUpdateUITests(unittest.TestCase):
    def test_routes_use_status_and_validate_dismissal_result(self):
        self.assertIn('/api/client-update-status',server.RequestHandler.GET_ROUTES)
        self.assertIn('/api/client-update-dismiss',server.RequestHandler.POST_ROUTES)
        handler=object.__new__(server.RequestHandler);handler.send_json=mock.Mock()
        with mock.patch.object(updates,'monitor_status',return_value={'last_checked':42}) as status:
            handler._get_api_client_update_status()
        handler.send_json.assert_called_with({'last_checked':42});status.assert_called_once_with()
        with mock.patch.object(updates,'dismiss_monitor_notice',return_value=False) as dismiss:
            handler._post_api_client_update_dismiss({'fingerprint':'stale'})
        self.assertFalse(handler.send_json.call_args.args[0]['success']);dismiss.assert_called_once_with('stale')

    def test_english_notifications_links_dismiss_and_stale_reply(self):
        source=ROOT/'TelegramManager.app/Contents/Resources/index.html'
        script=r'''
const text=require('fs').readFileSync(process.argv[1],'utf8');
const nodes={};const calls=[];
const document={getElementById:id=>nodes[id] ||= {style:{},classList:{add(){},remove(){}},textContent:'',innerHTML:''},querySelectorAll:()=>[]};
const esc=value=>String(value).replaceAll('<','&lt;').replaceAll('"','&quot;');
const toast=()=>{};
let status={last_checked:1791813600,next_check:1792418400,checking:false,notices:[{message:'New AyuGram build; compatibility unverified',url:'https://github.com/AyuGram/AyuGramDesktop/releases/tag/v7.3.2',evidence:{app_version:7003002,reader_rejects_newer:true,source_url:'https://raw.githubusercontent.com/AyuGram/AyuGramDesktop/v7.3.2/Telegram/SourceFiles/core/version.h'}}],notification_fingerprint:'new-build',dismissed:false};
let deferredReplies=null;
const api=async(route,method,body)=>{calls.push([route,method,body]);if(deferredReplies)return new Promise(resolve=>deferredReplies.push(resolve));if(route==='/api/client-update-dismiss'){if(body.fingerprint!=='new-build')throw Error('Wrong dismissal');status={...status,dismissed:true};return {success:true};}return status;};
eval(text.slice(text.indexOf('let clientUpdateBusy ='),text.indexOf('let pendingTelegramUpdate =')));
(async()=>{
 await loadClientUpdateStatus();
 if(nodes['client-update-notice'].style.display!=='flex')throw Error('Missing banner');
 if(!nodes['client-update-monitor'].innerHTML.includes('Mondays at 10:00 AM'))throw Error('Missing schedule');
 if(!nodes['client-update-monitor'].innerHTML.includes('Source AppVersion: 7003002'))throw Error('Unusable evidence');
 if(nodes['client-update-monitor'].innerHTML.includes('[object Object]'))throw Error('Evidence not formatted');
 if(safeClientUpdateLink('javascript:alert(1)')||safeClientUpdateLink('https://evil.example/release'))throw Error('Unsafe links');
 if(!safeClientUpdateLink(status.notices[0].url))throw Error('Official link rejected');
 await dismissClientUpdateNotice();
 if(nodes['client-update-notice'].style.display!=='none')throw Error('Dismissed banner remains');
 status={...status,notices:[],dismissed:false,error:'Offline; will retry',source_errors:['Official channel unavailable']};
 await loadClientUpdateStatus();
 if(!nodes['client-update-monitor'].innerHTML.includes('will retry'))throw Error('Missing offline status');
 deferredReplies=[];const stale=loadClientUpdateStatus();const fresh=loadClientUpdateStatus();
 deferredReplies[1]({...status,notices:[],dismissed:true});await fresh;
 deferredReplies[0]({...status,notices:[{message:'Stale notification'}],dismissed:false});await stale;
 if(nodes['client-update-notice'].style.display!=='none')throw Error('Stale reply restored dismissed notification');
 if(calls.some(call=>call[0].includes('install')))throw Error('Automatic installation attempted');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_background_monitor_starts_after_server_readiness(self):
        text=(ROOT/'TelegramManager.app/Contents/Resources/server.py').read_text()
        main=text[text.index('if __name__ == "__main__":'):]
        self.assertGreater(main.index('target=updates.run_weekly_monitor'),main.index('_write_ready_file(ready_file, port, SESSION_TOKEN)'))
