"""Run the actual page script and check its Nordic protobuf submission."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[3] / 'omi/src/portal.html'

@unittest.skipUnless(shutil.which('node'), 'node required for browser-script test')
class PortalPageTests(unittest.TestCase):
    def test_manual_ssid_uses_nordic_wire_format_and_reports_save_failure(self):
        script = PAGE.read_text().split('<script>')[1].split('</script>')[0]
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = Object.fromEntries(['setup','networks','ssid','password','host','port','key','status','save'].map(k=>[k,{value:'',add(){}}]));
Object.assign(nodes.ssid,{value:'Home'}); nodes.password.value='abcdefgh'; nodes.host.value='secondbrain.local'; nodes.port.value='7331'; nodes.key.value='01'.repeat(32);
let posts=[]; let fail=false;
const context={TextEncoder,TextDecoder,Uint8Array,console,Option:function(){},document:{getElementById:k=>nodes[k]},fetch:async(url,opts)=>{
 if(!opts)return {arrayBuffer:async()=>new ArrayBuffer(0)};
 posts.push([url,Array.from(opts.body)]);
 return {ok:!fail,text:async()=>fail?'Storage failure':'Saved'};
}};
vm.createContext(context);vm.runInContext(SCRIPT,context);
(async()=>{
 await nodes.setup.onsubmit({preventDefault(){}});
 assert.equal(posts.length,2); assert.equal(posts[0][0],'/omi/destination');
 assert.equal(posts[1][0],'/prov/configure');
 // NCS 2.9.0 common.proto: WifiConfig(wifi=1, passphrase=2),
 // WifiInfo(ssid=1, bssid=2, channel=4, auth=5 WPA_WPA2_PSK=4).
 assert.deepEqual(posts[1][1],[10,12,10,4,72,111,109,101,18,0,32,0,40,4,18,8,97,98,99,100,101,102,103,104]);
 assert.equal(nodes.status.textContent,'Saved'); assert.equal(nodes.password.value,'');
 nodes.password.value='abcdefgh';nodes.key.value='01'.repeat(32);fail=true;posts=[];
 await nodes.setup.onsubmit({preventDefault(){}});
 assert.equal(posts.length,1);assert.equal(nodes.save.disabled,false);
 assert.match(nodes.status.textContent,/Storage failure/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('SCRIPT', json.dumps(script))
        subprocess.run([shutil.which('node'),'-e',harness],check=True)
