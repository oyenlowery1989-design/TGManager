"""Selective Mac transfer UI, using only synthetic DOM nodes and API responses."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MacTransferUiTests(unittest.TestCase):
    def test_explicit_mapping_safe_rendering_failure_cleanup_and_busy_lock(self):
        script = r'''
const fs=require('fs'), assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'), elements=new Map(), calls=[], downloads=[], blobs=[], timers=[], revoked=[];
const setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
function node(tag='div') { const classes=new Set();return {tagName:tag,value:'',textContent:'',checked:false,disabled:false,children:[],style:{},
 setAttribute(k,v){this[k]=v},appendChild(x){this.children.push(x)},replaceChildren(){this.children=[]},focus(){},click(){downloads.push(this)},
 classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)}}; }
const document={getElementById(id){if(!elements.has(id))elements.set(id,node());return elements.get(id)},createElement:node};
const accounts=[{name:'<img src=x onerror=bad()>',path:'/synthetic/local',relpath:'target-folder'}, {name:'Second',path:'/synthetic/second'}];
const Blob=global.Blob, URL={createObjectURL(blob){blobs.push(blob);return 'synthetic:blob'},revokeObjectURL(url){revoked.push(url)}};
let pending=null, previewFails=false, workspaceRefreshes=0, previewIncludes={history:true,settings:true,organization:true};
async function loadWorkspaceBar(){workspaceRefreshes++;}
const archiveId='9007199254740993123';
const packageData={format:'telegram-manager-selective-transfer',version:1,accounts:[{private:'DO NOT RENDER RAW HISTORY'}]};
const previewAccount={id:'source-uuid',name:'<script>bad()</script>',folder:'<img source>',user_id:archiveId,deleted:2,edited:3,settings_count:4};
const api=async(path,method,body)=>{calls.push({path,method,body});if(pending)await pending;
 if(path.endsWith('/catalog'))return {success:true,history_available:true,settings_available:true,owners:[{user_id:archiveId,deleted:2,edited:3}]};
 if(path.endsWith('/preview'))return previewFails?{success:false,message:'<img failure>'}:{success:true,include:previewIncludes,accounts:[previewAccount],workspaces:[{name:'<script>workspace</script>',accounts:['source-uuid']}]};
 if(path.endsWith('/export'))return {success:true,package:packageData};
 if(path.endsWith('/import'))return {success:false,partial:true,message:'<img partial>',results:[{id:'source-uuid',path:'/synthetic/local',success:true,message:'<script>safe</script>',history:{imported:{deleted:2,edited:3},skipped:{deleted:0,edited:1},conflicts:1,ambiguities:0}}]};
 throw Error('Unexpected API '+path);
};
eval(source.slice(source.indexOf('let macTransfer ='),source.indexOf('// End selective Mac transfer.'))+`
(async()=>{
 showMacTransfer();
 assert.equal(calls.length,0,'Opening dialog does not inspect profiles or import anything');
 assert(transferElement('export').disabled);assert(transferElement('import').disabled);
 const exportRow=macTransfer.exports[0];
 assert.equal(transferElement('export-rows').children[0].children[0].children[1].textContent,accounts[0].name+' — target-folder');
 assert.equal(transferElement('export-rows').children[0].innerHTML,undefined);
 exportRow.selected=true;await loadTransferCatalog(exportRow);
 assert.equal(exportRow.select.value,'');assert(transferElement('export').disabled);
 const owners=exportRow.catalog.owners;exportRow.catalog.owners=[];renderMacTransfer();assert(!transferElement('export').disabled,'Empty initialized archives need no archive ID');exportRow.catalog.owners=owners;renderMacTransfer();
 exportRow.select.value=archiveId;exportRow.select.onchange();assert(!transferElement('export').disabled);
 await exportMacTransfer();assert.equal(downloads.length,1);
 assert.deepEqual(JSON.parse(await blobs[0].text()),packageData,'Download is the package, not the API response wrapper');
 assert.equal(await blobs[0].text(),JSON.stringify(packageData),'Compact download uses the same size basis as backend validation');
 assert.equal(revoked.length,0,'WebKit must get the blob before URL revocation');assert.equal(timers[0].ms,60000);timers[0].fn();assert.deepEqual(revoked,['synthetic:blob']);
 assert(transferElement('status').textContent.startsWith('Download started. Choose where to save'),'Do not claim saved before the native save dialog');
 const exportCall=calls.find(c=>c.path.endsWith('/export'));assert.equal(exportCall.body.accounts[0].user_id,archiveId);
 const input=transferElement('file');input.files=[{size:100,text:async()=>JSON.stringify(packageData)}];
 await previewMacTransfer(input);assert(macTransfer.package);assert.equal(input.value,'');
 let row=macTransfer.imports[0];assert.equal(row.target.value,'','Destination must never be guessed from source name');
 assert.equal(row.user.value,archiveId);assert(!row.check.checked);assert(!row.confirm.checked);
 assert.equal(row.target.children[1].textContent,accounts[0].name+' — target-folder');
 assert.equal(transferElement('import-rows').children[0].children[0].children[1].textContent,previewAccount.name+' — '+previewAccount.folder);
 assert(!JSON.stringify(transferElement('import-rows')).includes('DO NOT RENDER RAW HISTORY'));
 const beforeImport=calls.length;await importMacTransfer();assert.equal(calls.length,beforeImport);
 row.check.checked=true;row.target.value=accounts[0].path;row.target.onchange();renderMacTransfer();
 assert(transferElement('import').disabled,'Selecting rows and paths is insufficient for history');
 row.confirm.checked=true;renderMacTransfer();assert(!transferElement('import').disabled);
 row.user.value='bad';row.user.oninput();assert(!row.confirm.checked);assert(transferElement('import').disabled);
 row.user.value=archiveId;row.user.oninput();row.confirm.checked=true;renderMacTransfer();
 let release;pending=new Promise(r=>release=r);const importing=importMacTransfer();
 assert(transferElement('fields').disabled);assert(transferElement('close').disabled);
 hideMacTransfer();assert(macTransfer.package,'Busy close must preserve pending operation');
 const count=calls.length;await importMacTransfer();assert.equal(calls.length,count,'No duplicate submits');
 release();await importing;pending=null;
 const imported=calls.find(c=>c.path.endsWith('/import'));assert.deepEqual(imported.body.mappings,[{id:'source-uuid',path:'/synthetic/local',user_id:archiveId,confirmed:true}]);
 assert(transferElement('status').textContent.includes('Partially finished'));assert(transferElement('status').textContent.includes('<script>safe</script>'));
 assert(transferElement('status').textContent.includes('conflicts: 1'));assert.equal(transferElement('status').innerHTML,undefined);
 assert(!row.confirm.checked,'A completed attempt needs renewed history confirmation');assert.equal(workspaceRefreshes,1,'Refresh merged workspaces without replacing transfer results');
 previewFails=true;await previewMacTransfer(input);assert.equal(macTransfer.package,null);assert.equal(macTransfer.imports.length,0);
 assert(transferElement('import').disabled);assert.equal(transferElement('import-rows').children.length,0);
 assert.equal(transferElement('status').textContent,'<img failure>');
 const prior=calls.length;input.files=[{size:64*1024*1024+1,text:async()=>{throw Error('must not read')}}];await previewMacTransfer(input);
 assert.equal(calls.length,prior);assert(transferElement('status').textContent.includes('64 MiB'));
 input.files=[{size:10,text:async()=>'{broken'}];await previewMacTransfer(input);assert.equal(transferElement('status').textContent,'Invalid JSON file.');
 previewFails=false;input.files=[{size:100,text:async()=>JSON.stringify(packageData)}];await previewMacTransfer(input);
 row=macTransfer.imports[0];row.check.checked=true;row.target.value=accounts[0].path;transferElement('history').checked=false;renderMacTransfer();
 assert(!transferElement('import').disabled,'Settings and organization need explicit mapping, not history confirmation');
 macTransfer.imports.push({...row,id:'other'});renderMacTransfer();assert(transferElement('import').disabled,'Duplicate destination rejected');macTransfer.imports.pop();
 previewIncludes={history:false,settings:false,organization:true};await previewMacTransfer(input);
 assert(transferElement('history').disabled);assert(!transferElement('history').checked);
 assert(transferElement('settings').disabled);assert(!transferElement('settings').checked);
 assert(!transferElement('organization').disabled);assert(transferElement('organization').checked);
 assert(transferElement('export').disabled,'Import category choices must not export source accounts accidentally');
 row=macTransfer.imports[0];row.check.checked=true;row.target.value=accounts[0].path;renderMacTransfer();
 assert(!transferElement('import').disabled,'Organization-only preview can import without unsupported categories');
 const sourceCheck=transferElement('export-rows').children[0].children[0].children[0];sourceCheck.checked=false;await sourceCheck.onchange();
 assert.equal(macTransfer.package,null);assert(!transferElement('history').disabled);assert(!transferElement('settings').disabled);
 assert(transferElement('history').checked,'Returning to export restores the prior source category choices rather than import choices');
 clearMacTransfer();showMacTransfer();
 assert(transferElement('history').checked);assert(transferElement('settings').checked);assert(transferElement('organization').checked);
 assert(!transferElement('history').disabled);assert(!transferElement('settings').disabled);
 transferElement('history').checked=false;transferElement('settings').checked=false;await changeTransferInclude();
 const orgSource=transferElement('export-rows').children[0].children[0].children[0];orgSource.checked=true;
 const beforeCatalog=calls.length;await orgSource.onchange();assert.equal(calls.length,beforeCatalog,'Organization-only selection must not inspect AyuGram');
 assert(!transferElement('export').disabled,'Organization-only export does not depend on a history catalog');
 transferElement('history').checked=true;await changeTransferInclude();assert.equal(calls.length,beforeCatalog+1,'Adding history explicitly inspects selected sources');
 assert(transferElement('export').disabled,'Populated history still requires an explicit archive ID');
 previewIncludes={history:true,settings:true,organization:true};
 pending=new Promise(r=>release=r);const loading=previewMacTransfer(input);await Promise.resolve();assert(macTransfer.busy);clearMacTransfer();release();await loading;pending=null;
 assert.equal(macTransfer.package,null,'Late responses cannot revive a cleared package');assert.equal(macTransfer.imports.length,0);
 assert(!transferElement('modal').classList.contains('active'));
 document.getElementById('lock-screen').classList.add('visible');showMacTransfer();assert(!transferElement('modal').classList.contains('active'));
 assert(!calls.some(c=>/open|close|login/.test(c.path)));
 assert(source.includes('function lockApp() {\\n  clearMacTransfer();'),'Lock clears transfer memory');
 assert(source.includes('if (transferElement("modal").classList.contains("active")) { hideMacTransfer(); return; }'),'Escape preserves underlying settings');
})().catch(e=>{console.error(e);process.exitCode=1});`);
'''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'TelegramManager.app/Contents/Resources/index.html')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
