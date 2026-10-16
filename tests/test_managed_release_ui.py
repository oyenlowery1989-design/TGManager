"""Run the update UI against synthetic DOM and endpoint replies, never real apps."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources/index.html'
HARNESS = r'''
const assert = require('assert');
const text = require('fs').readFileSync(process.argv[1], 'utf8');
const nodes = {}, calls = [], notifications = [];
let releaseButtons = [];
function node(id) {
  if (!nodes[id]) nodes[id] = {
    style:{}, disabled:true, dataset:{}, textContent:'', _html:'',
    classList:{add(){},remove(){}},
    set innerHTML(value) {
      this._html = value;
      if (id !== 'client-update-list') return;
      releaseButtons = [...value.matchAll(/<button\s+([^>]+)>/g)].map(match => ({
        className: match[1].match(/class="([^"]+)"/)?.[1] || '',
        dataset:{available:match[1].match(/data-available="([^"]+)"/)?.[1]},
        disabled: /\sdisabled(?:\s|$)/.test(match[1]),
      }));
    },
    get innerHTML() { return this._html; },
  };
  return nodes[id];
}
const document = {
  getElementById:node,
  querySelectorAll:selector => releaseButtons.filter(button => button.className.split(' ').includes(selector.slice(1))),
};
const esc = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const toast = (...args) => notifications.push(args);
const toastResult = (...args) => notifications.push(args);
const loadSharedAppStatus = () => {};
const refresh = async () => {};
let rows = [
  {client:'Telegram',installed_version:'7.0',version:'7.1',update_available:true,release_url:'https://github.com/telegramdesktop/tdesktop/releases/tag/v7.1'},
  {client:'Manager',component:'manager',managed_release:true,installed_version:'1.1.0',version:'1.2.0',update_available:true,release_url:'https://github.com/oyenlowery1989-design/TGManager/releases/tag/manager-v1.2.0'},
  {client:'AyuGram Custom',component:'ayugram-custom',managed_release:true,installed_version:'1.0.0',version:'1.1.0',update_available:true},
];
let status = {success:true,job:{},rollback:{manager:true,'ayugram-custom':false},clients:rows.filter(row=>row.managed_release),last_checked:42};
let override = null;
const api = async (route, method, body) => {
  calls.push([route,method,body]);
  if (override) {
    const result = override(route,method,body);
    if (result !== undefined) return result;
  }
  if (route === '/api/check-client-updates') return {success:true,clients:rows,warning:'Official compatibility warning'};
  if (route === '/api/managed-release-status') return status;
  if (route === '/api/schedule-managed-release') {
    status = {...status,job:{status:'queued',component:body.component,message:'Scheduled. Quit Manager and all clients normally.'}};
    return {success:true,scheduled:true,message:status.job.message};
  }
  if (route === '/api/cancel-managed-release') {
    status = {...status,job:{status:'cancelled',message:'Scheduled update cancelled.'}};
    return {success:true,message:status.job.message};
  }
  if (route === '/api/install-client-update') return {success:true};
  if (route === '/api/client-update-status') return {notices:[],dismissed:true};
  throw Error('Unexpected route: ' + route);
};
eval(text.slice(text.indexOf('let clientUpdateBusy ='),text.indexOf('let pendingTelegramUpdate =')) + '\nfunction snapshotRows(){return clientReleaseRows;}');
const managedButtons = () => releaseButtons.filter(button => button.className.includes('managed-release-schedule'));
'''


class ManagedReleaseUITests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run(['node', '-e', HARNESS + '\n(async()=>{\n' + body + '\n})().catch(error=>{console.error(error);process.exitCode=1});', str(SOURCE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_schedule_cancel_rollback_and_installed_version(self):
        self.run_js(r'''
await showClientUpdates();
assert(nodes['client-update-list'].innerHTML.includes('Download and Schedule'));
assert(nodes['client-update-list'].innerHTML.includes('Download and Install'));
assert(nodes['client-update-warning'].textContent.includes('Official compatibility warning'));
assert.equal(nodes['managed-release-notice'].style.display,'flex');
assert.equal(nodes['managed-release-cancel'].disabled,true);
assert.equal(nodes['managed-release-rollback-manager'].disabled,false);
assert.equal(nodes['managed-release-rollback-custom'].disabled,true);
assert(managedButtons().every(button=>!button.disabled));
const before = snapshotRows().find(row=>row.component==='manager').installed_version;
let resolveSchedule;
override = route => route === '/api/schedule-managed-release' ? new Promise(resolve=>resolveSchedule=resolve) : undefined;
const scheduling = scheduleManagedRelease('manager');
assert.equal(nodes['client-update-check'].disabled,true);
assert(managedButtons().every(button=>button.disabled));
assert.equal(nodes['managed-release-rollback-manager'].disabled,true);
assert.equal(nodes['managed-release-cancel'].disabled,true);
const count = calls.length;
await scheduleManagedRelease('ayugram-custom');
assert.equal(calls.length,count,'Busy scheduling accepted a second job');
status = {...status,job:{status:'queued',component:'manager',message:'Scheduled only'}};
resolveSchedule({success:true,scheduled:true,message:'Scheduled only'});
await scheduling;
override = null;
assert.equal(snapshotRows().find(row=>row.component==='manager').installed_version,before);
assert.equal(nodes['managed-release-cancel'].disabled,false);
assert.equal(nodes['managed-release-rollback-manager'].disabled,true);
assert.equal(nodes['managed-release-notice'].style.display,'none');
assert(managedButtons().every(button=>button.disabled));
assert(nodes['managed-release-status'].textContent.includes('queued'));
assert.deepEqual(calls.find(call=>call[0]==='/api/schedule-managed-release')[2],{component:'manager',version:'1.2.0'});
await cancelManagedRelease();
assert.equal(nodes['managed-release-cancel'].disabled,true);
assert.equal(nodes['managed-release-rollback-manager'].disabled,false);
assert(nodes['managed-release-status'].textContent.includes('cancelled'));
assert.deepEqual(calls.find(call=>call[0]==='/api/cancel-managed-release')[2],{});
const blocked = calls.length;
await scheduleManagedRelease('ayugram-custom',true);
assert.equal(calls.length,blocked,'Unavailable rollback was submitted');
await scheduleManagedRelease('manager',true);
assert.deepEqual(calls.filter(call=>call[0]==='/api/schedule-managed-release').at(-1)[2],{component:'manager',rollback:true});
assert.equal(snapshotRows().find(row=>row.component==='manager').installed_version,before);
assert(!calls.some(call=>call[0]==='/api/install-client-update'));
assert(!notifications.some(args=>args[0]==='Update installed'));
''')

    def test_escape_links_failures_and_official_isolation(self):
        self.run_js(r'''
rows[1] = {...rows[1],client:"Manager');evil()",note:'<img src=x onerror=evil()>',release_url:'javascript:evil()'};
rows[2] = {...rows[2],error:'<script>evil()</script>',release_url:'https://evil.example/release'};
status = {...status,clients:rows.filter(row=>row.managed_release)};
await showClientUpdates();
const html = nodes['client-update-list'].innerHTML;
assert(html.includes('&lt;img'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('<img')&&!html.includes('<script>'));
assert(!html.includes('href="javascript:')&&!html.includes('href="https://evil.example'));
assert(!html.includes("onclick=\"installClientUpdate('Manager"));
assert(html.includes('onclick="scheduleManagedRelease(clientReleaseRows['));
for (const url of ['javascript:alert(1)','https://github.com.evil.example/oyenlowery1989-design/TGManager/releases','https://github.com/oyenlowery1989-design/TGManager-malicious/releases','https://user:secret@github.com/oyenlowery1989-design/TGManager/releases','https://github.com:8443/oyenlowery1989-design/TGManager/releases']) assert.equal(safeClientUpdateLink(url),'');
assert(safeClientUpdateLink('https://github.com/oyenlowery1989-design/TGManager/releases/tag/manager-v1.2.0'));
assert(safeClientUpdateLink('https://github.com/oyenlowery1989-design/TGManager'));
assert.equal(managedButtons()[1].disabled,true,'Failed custom row enabled');
override = route => route === '/api/schedule-managed-release' ? {success:false,message:'<b>Cannot stage</b>'} : undefined;
await scheduleManagedRelease('manager');
assert.equal(nodes['managed-release-status'].textContent,'<b>Cannot stage</b>');
assert.equal(nodes['managed-release-cancel'].disabled,true);
assert.equal(snapshotRows().find(row=>row.component==='manager').installed_version,'1.1.0');
assert.equal(nodes['client-update-check'].disabled,false);
override = route => route === '/api/managed-release-status' ? {_networkError:true} : undefined;
await loadManagedReleaseStatus();
assert(nodes['managed-release-status'].textContent.includes('unavailable'));
assert(managedButtons().every(button=>button.disabled));
assert.equal(nodes['managed-release-rollback-manager'].disabled,true);
override = null;
await loadManagedReleaseStatus();
await installClientUpdate('Telegram');
assert(calls.some(call=>call[0]==='/api/install-client-update'));
assert.equal(snapshotRows().find(row=>row.client==='Telegram').installed_version,'7.1');
assert.equal(snapshotRows().find(row=>row.component==='manager').installed_version,'1.1.0');
const count = calls.length;
await installClientUpdate("Manager');evil()");
assert.equal(calls.length,count,'Managed row reached official installer');
''')

    def test_stale_status_cancel_error_and_busy_recovery(self):
        self.run_js(r'''
await showClientUpdates();
const replies = [];
override = route => route === '/api/managed-release-status' ? new Promise(resolve=>replies.push(resolve)) : undefined;
const stale = loadManagedReleaseStatus();
const fresh = loadManagedReleaseStatus();
replies[1]({...status,job:{status:'cancelled',message:'Fresh cancellation'}});
await fresh;
replies[0]({...status,job:{status:'queued',component:'manager',message:'Old queue'}});
await stale;
assert(nodes['managed-release-status'].textContent.includes('Fresh cancellation'));
assert.equal(nodes['managed-release-cancel'].disabled,true);
override = null;
await scheduleManagedRelease('manager');
override = route => {
  if (route === '/api/cancel-managed-release') throw Error('Cancellation offline');
};
await cancelManagedRelease();
assert.equal(nodes['managed-release-status'].textContent,'Cancellation offline');
assert.equal(nodes['managed-release-cancel'].disabled,false,'Failed cancel must preserve queue');
assert.equal(nodes['client-update-check'].disabled,false,'Busy state stuck after exception');
override = null;
await cancelManagedRelease();
override = route => route === '/api/schedule-managed-release' ? {success:true,scheduled:false,message:'Not queued'} : undefined;
await scheduleManagedRelease('manager');
assert.equal(nodes['managed-release-status'].textContent,'Not queued');
assert.equal(nodes['managed-release-cancel'].disabled,true);
assert.equal(nodes['client-update-check'].disabled,false);
''')


if __name__ == '__main__':
    unittest.main()
