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
        start=source.index('  if (acc.status === "ready") {',source.index('function buildCard'))
        end=source.index('  const healthTip',start)
        block=source[start:end]
        script='''const esc=x=>x;
function buttons(profile,running=false){const acc={status:'ready',path:'/test',ayugram_has_profile:profile,ayugram_running:running};const actions=[];
'''+block+'''return actions.join('');}
if(!buttons(false).includes('New AyuGram Login'))throw Error('Missing first login');
if(buttons(true).includes('New AyuGram Login'))throw Error('Redundant login button');
if(!buttons(true).includes('Open with AyuGram'))throw Error('Cannot resume existing profile');
if(!buttons(true,true).includes('Close AyuGram')||buttons(true,true).includes('New AyuGram Login'))throw Error('Running controls incorrect');
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
