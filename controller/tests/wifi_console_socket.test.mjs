// Execute the wizard's actual config probe against both provisioning histories.
import {readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import assert from 'node:assert/strict';
const src = readFileSync(new URL('../static/dashboard.jsx', import.meta.url), 'utf8');
const start = src.indexOf('  async function consoleWpaCommand(');
const end = src.indexOf('  async function scanWifiConsole(', start);
const {consoleWpaCommand} = await import('data:text/javascript;base64,' + Buffer.from(src.slice(start,end) + '\nexport {consoleWpaCommand};').toString('base64'));
const root = mkdtempSync(join(tmpdir(),'em-wifi-test-'));
const own = join(root,'own.conf'), android = join(root,'android.conf');
const con = {async run(script) {
  script = script.replaceAll('/data/emos/wpa.conf',own).replaceAll('/data/misc/wifi/wpa_supplicant.conf',android);
  const result = spawnSync('sh',['-c',script],{encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  return result.stdout;
}};
try {
  writeFileSync(android,'ctrl_interface=DIR=/data/misc/wifi/sockets GROUP=wifi\n');
  assert.equal(await consoleWpaCommand(con),"wpa_cli -p '/data/misc/wifi/sockets' -i wlan0");
  writeFileSync(own,'ctrl_interface=/data/emos/sockets\n');
  assert.equal(await consoleWpaCommand(con),"wpa_cli -p '/data/emos/sockets' -i wlan0");
  writeFileSync(own,'ctrl_interface=/first\n  ctrl_interface=DIR=/last GROUP=wifi\n');
  assert.equal(await consoleWpaCommand(con),"wpa_cli -p '/last' -i wlan0");
  writeFileSync(own,'update_config=1\n');
  assert.equal(await consoleWpaCommand(con),"wpa_cli -p '/data/misc/wifi/sockets' -i wlan0");
  writeFileSync(own,"ctrl_interface=/tmp/quote'path\n");
  const cmd=await consoleWpaCommand(con);
  const probe=spawnSync('sh',['-c',`wpa_cli() { printf '%s\\n' "$@"; }; ${cmd} status`],{encoding:'utf8'});
  assert.equal(probe.stdout,"-p\n/tmp/quote'path\n-i\nwlan0\nstatus\n");
  await assert.rejects(consoleWpaCommand({async run(){return 'connection lost';}}));
  for (const name of ['scanWifiConsole','runEmosWifi']) {
    const i=src.indexOf(`  async function ${name}(`);
    const body=src.slice(i,src.indexOf('\n  async function ',i+5));
    assert.match(body,/await consoleWpaCommand\(con\)/);
    assert.ok(!body.includes('wpa_cli -p /data/misc/wifi/sockets'));
  }
  console.log('wifi_console_socket: all checks passed');
} finally {rmSync(root,{recursive:true,force:true});}
