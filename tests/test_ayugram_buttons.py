import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RESOURCES = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'
sys.path.insert(0, str(RESOURCES))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state


class AyuGramButtonTests(unittest.TestCase):
    def test_open_feedback_survives_refresh_and_clears_after_failure_or_success(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const calls = [], notices = [], timers = [];
const account = (path, profile = true) => ({
  path, name:path, rel_path:path, status:'ready', ayugram_has_profile:profile,
  running:false, telegram_running:false, ayugram_running:false
});
const context = {
  accounts:[account('/one'), account('/two')],
  btoa:s=>Buffer.from(s, 'binary').toString('base64'), unescape, encodeURIComponent,
  _keeperInfo:{enabled:false}, pendingNoteEdits:{},
  api:(endpoint, method, data)=>new Promise((resolve, reject)=>calls.push({endpoint, data, resolve, reject})),
  toast:(message, type)=>notices.push({message, type}),
  setTimeout:fn=>timers.push(fn),
  mergeLocalEdits:()=>{},
  renderAccounts:()=>{ context.html = context.accounts.map(a=>context.buildCard(a)).join(''); },
  refresh:async()=>{ context.renderAccounts(); }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function esc(s)'), source.indexOf('// ── Rendering')), context);
vm.runInContext(source.slice(source.indexOf('const COLOR_PALETTE ='), source.indexOf('let _statsData =')), context);
vm.runInContext(source.slice(source.indexOf('const _openingPaths ='), source.indexOf('\nasync function openAll()')), context);
const busyButton = html => /<button\b[^>]*disabled[^>]*>[^<]*Opening AyuGram…<\/button>/.test(html);
(async()=>{
  context.accounts[0].running = context.accounts[0].telegram_running = true;
  const first = context.openAccount('/one', 'AyuGram');
  assert(busyButton(context.html || ''), 'Click must immediately show a disabled Opening AyuGram button');
  assert(/aria-busy="true"/.test(context.html), 'Opening state must be accessible');
  assert(notices.some(n=>n.message.includes('Opening AyuGram')), 'Immediate feedback missing');
  context.renderAccounts(); // The normal account poll must not erase progress.
  assert(busyButton(context.buildCard(context.accounts[0])));
  assert(!busyButton(context.buildCard(context.accounts[1])), 'Other account was disabled');
  await context.openAccount('/one', 'AyuGram');
  assert.equal(calls.length, 1, 'Double-click must not issue another launch');
  calls[0].resolve({success:false, message:'Synthetic launch failure'});
  await first;
  assert(!busyButton(context.html));
  assert(context.html.includes('Open with AyuGram'));
  assert(notices.some(n=>n.type==='error' && n.message==='Synthetic launch failure'));

  const retry = context.openAccount('/one', 'AyuGram');
  assert(busyButton(context.html));
  calls[1].resolve({success:true, message:'AyuGram opened'});
  await retry;
  assert(!busyButton(context.html));
  // Telegram being open must not hide an AyuGram liveness failure.
  await timers.pop()();
  assert(notices.some(n=>n.type==='warning' && n.message.includes('AyuGram launched')));

  const network = context.openAccount('/one', 'AyuGram');
  calls[2].reject(new Error('Synthetic connection failure'));
  await network;
  assert(!busyButton(context.html));
  assert(notices.some(n=>n.type==='error' && n.message.includes('Synthetic connection failure')));

  context.accounts[1].ayugram_has_profile = false;
  const login = context.openAccount('/two', 'AyuGram', true);
  assert(busyButton(context.buildCard(context.accounts[1])));
  assert(!context.buildCard(context.accounts[1]).includes('New AyuGram Login'));
  assert.equal(calls[3].endpoint, '/api/new-ayugram-login');
  calls[3].resolve({success:false, message:'Login failed'});
  await login;
  assert(context.buildCard(context.accounts[1]).includes('New AyuGram Login'));
})().catch(error=>{ console.error(error); process.exitCode=1; });
'''
        result = subprocess.run(['node', '-e', script, str(RESOURCES / 'index.html')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scan_distinguishes_existing_profile_including_pending_login(self):
        with tempfile.TemporaryDirectory() as temp:
            account=Path(temp)/'Account'
            (account/'TelegramForcePortable/tdata').mkdir(parents=True)
            with mock.patch.object(server,'ROOT_DIR',temp), mock.patch.object(state,'ROOT_DIR',temp), \
                 mock.patch.object(server,'get_shared_app',return_value=None), \
                 mock.patch.object(server,'_get_ps_output',return_value='1 /unrelated'):
                self.assertFalse(server.scan_accounts()[0]['ayugram_has_profile'])
                (account/'.ayugram/TelegramForcePortable/tdata').mkdir(parents=True)
                self.assertTrue(server.scan_accounts()[0]['ayugram_has_profile'])

    def test_buttons_show_new_login_only_without_existing_profile(self):
        source=(RESOURCES/'index.html').read_text()
        start=source.index('  const actions = [];',source.index('function buildCard'))
        end=source.index('  const healthTip',start)
        block=source[start:end]
        script='''const esc=x=>x;
const _openingPaths = new Map();
function buttons(profile,running=false){const acc={status:'ready',path:'/test',ayugram_has_profile:profile,ayugram_running:running};
'''+block+'''return actions.join('');}
if(!buttons(false).includes('New AyuGram Login'))throw Error('Missing first login');
if(buttons(true).includes('New AyuGram Login'))throw Error('Redundant login button');
if(!buttons(true).includes('Open with AyuGram'))throw Error('Cannot resume existing profile');
if(!buttons(true,true).includes('Close AyuGram')||buttons(true,true).includes('New AyuGram Login'))throw Error('Running controls incorrect');
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
