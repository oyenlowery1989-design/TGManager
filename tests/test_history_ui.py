"""English History Sync controls, synthetic DOM and API only."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HistoryUiTests(unittest.TestCase):
    def test_opt_in_confirmation_busy_state_lock_and_plain_text(self):
        script = r'''
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8'), elements = new Map(), calls = [], intervals = [];
function node() {const classes = new Set(); return {value:'',textContent:'',checked:false,disabled:false,style:{},children:[],
  appendChild(x){this.children.push(x)},replaceChildren(){this.children=[]},
  classList:{contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x)}}}
const document = {getElementById(id){if(!elements.has(id)) elements.set(id,node());return elements.get(id)},createElement:node};
const window = {addEventListener(){}}, setInterval=(fn,ms)=>{intervals.push({fn,ms});return intervals.length};
const accounts = [{path:'/synthetic/profile',name:'<script>name</script>'}];
let bindings = [], pending = null;
const api = async(path,method,body) => {
 calls.push({path,method,body}); if(pending) await pending;
 if(path.endsWith('/status')) return {success:true,bindings};
 if(path.endsWith('/catalog')) return {success:true,local:[{user_id:'123',deleted:1,edited:0}],remote:[{user_id:'123',deleted:2,edited:1}]};
 if(path.endsWith('/configure')) bindings = [{path:body.path,user_id:body.user_id,enabled:true,state:'idle',message:'Enabled'}];
 if(path.endsWith('/sync')) return {success:true,state:'deferred',message:'Waiting for local AyuGram to close.'};
 if(path.endsWith('/disable')) bindings[0].enabled=false;
 return {success:true,message:'Done'};
};
eval(source.slice(source.indexOf('let remoteHistoryBusy'),source.indexOf('let clientUpdateBusy')) + `
(async()=>{
 await loadRemoteHistory();
 assert.equal(historyElement('account').value,'','Never select or enable a real profile automatically');
 assert.equal(historyElement('configure').disabled,true);
 assert.equal(historyElement('account').children[1].textContent,accounts[0].name);
 assert.equal(historyElement('account').children[1].innerHTML,undefined);
 historyElement('account').value=accounts[0].path;selectRemoteHistory();
 historyElement('user').value='123';historyElement('confirm').checked=true;renderRemoteHistory();
 assert.equal(historyElement('configure').disabled,true,'Catalog must establish both archive IDs first');
 await remoteHistoryAction('catalog');
 assert.equal(historyElement('configure').disabled,false);
 await remoteHistoryAction('configure');
 assert.equal(historyElement('sync').disabled,false);
 assert.equal(historyElement('confirm').checked,false);
 assert(calls.some(x=>x.path.endsWith('/configure')&&x.body.user_id==='123'&&x.body.confirmed));
 await remoteHistoryAction('sync');
 assert(historyElement('status').textContent.includes('Waiting for local AyuGram'));
 let release;pending=new Promise(r=>release=r);
 const syncing=remoteHistoryAction('sync');
 assert.equal(historyElement('account').disabled,true);
 const count=calls.length;await remoteHistoryAction('sync');assert.equal(calls.length,count);
 release();await syncing;pending=null;
 bindings[0].state='error';bindings[0].message='<img src=x onerror=bad()>';await loadRemoteHistory();
 assert.equal(historyElement('notice').style.display,'flex');
 assert(historyElement('notice-text').textContent.includes('<img'));
 assert.equal(historyElement('notice-text').innerHTML,undefined);
 await remoteHistoryAction('disable');assert.equal(historyElement('sync').disabled,true);
 const before=calls.length;document.getElementById('lock-screen').classList.add('visible');
 await loadRemoteHistory();assert.equal(calls.length,before,'Locked status polling must not refresh idle lock');
 assert(!calls.some(x=>x.path.includes('open')),'History UI never launches an account or viewer');
 startRemoteHistoryMonitor();startRemoteHistoryMonitor();assert.equal(intervals.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});`);
'''
        result = subprocess.run(['node','-e',script,str(ROOT/'TelegramManager.app/Contents/Resources/index.html')],
                                capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
