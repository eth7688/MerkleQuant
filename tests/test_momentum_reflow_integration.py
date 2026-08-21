import importlib
import json
import subprocess
import time
import unittest
from threading import Event
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


with patch("trader.SqueezeBreakoutBot.start"):
    importlib.import_module("web_ui")


def wait_until_idle(web_ui, timeout=2.0):
    deadline = time.monotonic() + timeout
    while web_ui.state["scanning"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return not web_ui.state["scanning"]


def wait_until_payload_and_idle(web_ui, payload, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if web_ui.cache.get("reflow_1h") == payload and not web_ui.state["scanning"]:
            return True
        time.sleep(0.01)
    return False


def render_reflow_payload(payload):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    helpers = source[
        source.index("function escapeRHtml(value)"):
        source.index("function fmtRValue(value,signed)")
    ]
    renderer = source[
        source.index("var _reflowFilters="):
        source.index("// ===== SCANNING =====")
    ]
    script = f"""
var nodes={{stats:{{innerHTML:''}},main:{{innerHTML:''}}}};
global.localStorage={{getItem:function(){{return null;}},setItem:function(){{}}}};
global.window={{}};
global.document={{getElementById:function(id){{return nodes[id];}}}};
{helpers}
{renderer}
renderMomentumReflow({json.dumps(payload)});
process.stdout.write(JSON.stringify(nodes));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def render_reflow_filtered_payload(payload, filter_name, filter_value):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    helpers = source[
        source.index("function escapeRHtml(value)"):
        source.index("function fmtRValue(value,signed)")
    ]
    renderer = source[
        source.index("var _reflowFilters="):
        source.index("// ===== SCANNING =====")
    ]
    script = f"""
var nodes={{stats:{{innerHTML:''}},main:{{innerHTML:''}}}};
global.window={{}};
global.localStorage={{getItem:function(){{return null;}},setItem:function(){{}}}};
global.document={{getElementById:function(id){{return nodes[id];}}}};
{helpers}
{renderer}
var payload={json.dumps(payload)};
renderMomentumReflow(payload);
setReflowFilter({json.dumps(filter_name)}, {json.dumps(filter_value)});
process.stdout.write(JSON.stringify({{nodes:nodes,sourceLength:payload.rows.length}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def poll_reflow_payload(initial_payload, updated_payload):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    lifecycle = source[
        source.index("function show(tab, btn)"):
        source.index("// ===== TRADER PANEL =====")
    ]
    script = f"""
var D={{reflow_1h:{json.dumps(initial_payload)}}};
var cur='squeeze_4h', pollTimer=null, traderPoll=null, btcPoll=null, demoPoll=null;
var _lastTraderData=null, _traderInitDone=false, _reflowPolling=false, _pollingScan=false;
var rendered=[];
var nodes={{}};
global.document={{
  querySelectorAll:function(){{return []; }},
  getElementById:function(id){{return nodes[id]||(nodes[id]={{style:{{}},textContent:'',className:'',disabled:false}});}}
}};
global.clearInterval=function(){{}};
global.setInterval=function(fn){{global.pollFn=fn;return 7;}};
global.setTimeout=function(){{}};
global.stopEngineHeartbeat=function(){{}};
global.setDesc=function(){{}};
global.renderMomentumReflow=function(payload){{rendered.push(payload.rows[0].symbol);}};
global.fetch=function(){{return Promise.resolve({{json:function(){{return Promise.resolve({{data:{{reflow_1h:{json.dumps(updated_payload)}}},time:'12:00',status:'',progress:'',scanning:false}});}}}});}};
{lifecycle}
show('reflow_1h', null);
Promise.resolve().then(function(){{return Promise.resolve();}}).then(function(){{
  process.stdout.write(JSON.stringify({{renders:rendered,interval:pollTimer}}));
}});
"""
    completed = subprocess.run(
        ["node", "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=script,
    )
    return json.loads(completed.stdout)


def run_alert_sound_javascript(test_body, include_menu_switch=False):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    if "var REFLOW_ALERT_SOUND_KEY=" not in source:
        raise AssertionError("reflow alert sound JavaScript is missing")
    script = source[
        source.index("var REFLOW_ALERT_SOUND_KEY="):
        source.index("// ===== TRADER PANEL =====")
    ]
    if include_menu_switch:
        script += source[
            source.index("function selectTab(tid)"):
            source.index("function copySymbol(sym, el)")
        ]
    harness = """
const assert=require('assert');
let storage={};
global.localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>storage[k]=String(v)};
let fetchPayload={latest_alert_id:0,events:[]},fetches=[];
global.fetch=url=>{fetches.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(fetchPayload)});};
global.document={getElementById:()=>null}; global.window=global;
global.setInterval=(fn,delay)=>{global.alertPoll=fn;global.alertPollDelay=delay;return 9;};
global.clearedTimer=null;global.clearInterval=id=>{global.clearedTimer=id;};
"""
    try:
        completed = subprocess.run(
            ["node", "-"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=harness + script + test_body,
        )
    except subprocess.CalledProcessError as error:
        raise AssertionError(error.stderr) from error
    return completed.stdout


def first_use_scan_label(cache):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    marker = "var firstMenu=document.querySelector('.menu-items');"
    initial_state = source[
        source.index(marker):source.index("initReflowAlertSound();", source.index(marker))
    ]
    script = f"""
var D={json.dumps(cache)};
var nodes={{scanLabel:{{textContent:''}}}};
global.document={{
  querySelector:function(){{return null;}},
  getElementById:function(id){{return nodes[id];}}
}};
{initial_state}
process.stdout.write(nodes.scanLabel.textContent ? 'prompt' : 'empty');
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def reset_scan_admission(web_ui):
    web_ui.state.update(scanning=False, progress="", text="")
    for worker_name in ("_reflow_worker", "_scan_worker"):
        setattr(web_ui, worker_name, None)


def candidate():
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "instrument_type": "CRYPTO",
        "breakout_time": 100,
        "breakout_close_time": 3_600_100,
        "return_open_time": 1_000,
        "price": 100.0,
        "ema50": 99.0,
        "window_index": 1,
        "daily_kind": "strong_momentum",
        "daily_rank": 3,
        "breakout_volume_ratio": 3.0,
        "max_expansion_atr": 3.0,
        "close_distance_atr": 0.0,
    }


class MomentumReflowUiTests(unittest.TestCase):
    def test_successful_scan_observes_alerts_after_history_merge(self):
        web_ui = importlib.import_module("web_ui")
        payload = {
            "rows": [{
                **candidate(),
                "signal_key": "key",
                "quality_label": "HIGH",
                "status": "ACTIVE",
            }]
        }
        with patch.object(web_ui, "scan_momentum_reflow", return_value={"rows": []}), \
             patch.object(web_ui, "merge_reflow_signals", return_value=payload), \
             patch.object(web_ui, "observe_reflow_alerts", return_value=[{"alert_id": 1}]) as observe:
            result = web_ui._run_reflow_scan(lambda *_: None)

        self.assertIs(result, payload)
        observe.assert_called_once()

    def test_alert_ledger_failure_does_not_fail_scan(self):
        web_ui = importlib.import_module("web_ui")
        payload = {"rows": []}
        with patch.object(web_ui, "scan_momentum_reflow", return_value={"rows": []}), \
             patch.object(web_ui, "merge_reflow_signals", return_value=payload), \
             patch.object(
                 web_ui,
                 "observe_reflow_alerts",
                 side_effect=ValueError("broken ledger"),
             ):
            self.assertIs(web_ui._run_reflow_scan(lambda *_: None), payload)

        self.assertIn("broken ledger", web_ui._reflow_alert_status["last_error"])

    def test_alert_observation_error_masks_webhook_and_caps_status(self):
        web_ui = importlib.import_module("web_ui")
        unsafe_error = (
            "ledger failure https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
            "key=leaked-observation-key " + "x" * 100
        )

        with patch.object(
            web_ui,
            "observe_reflow_alerts",
            side_effect=ValueError(unsafe_error),
        ):
            self.assertEqual(web_ui._process_reflow_alerts({"rows": []}, 1), [])

        status_error = web_ui._reflow_alert_status["last_error"]
        self.assertNotIn("leaked", status_error)
        self.assertLessEqual(len(status_error), 80)

    def test_alert_api_requires_login_and_returns_no_webhook(self):
        web_ui = importlib.import_module("web_ui")
        client = web_ui.app.test_client()

        self.assertEqual(client.get("/api/reflow/alerts?after=0").status_code, 401)
        with client.session_transaction() as user_session:
            user_session["user_id"] = 9
        with patch.object(
            web_ui,
            "read_public_alerts",
            return_value={"latest_alert_id": 3, "events": []},
        ):
            response = client.get("/api/reflow/alerts?after=2")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("webhook", response.get_data(as_text=True).lower())

    def test_alert_api_reports_corrupt_ledger_as_unavailable(self):
        web_ui = importlib.import_module("web_ui")
        client = web_ui.app.test_client()
        with client.session_transaction() as user_session:
            user_session["user_id"] = 9
        with patch.object(
            web_ui,
            "read_public_alerts",
            side_effect=ValueError("broken ledger"),
        ):
            response = client.get("/api/reflow/alerts?after=0")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("broken ledger", response.get_data(as_text=True))

    def test_sound_first_enable_baselines_without_playing_and_batch_plays_once(self):
        body = """
let plays=0,activations=0;
activateReflowAudio=()=>{activations++;return Promise.resolve(true);};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:4,events:[]};await setReflowSoundEnabled(true);
assert.equal(activations,1);assert.equal(plays,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'4');
fetchPayload={latest_alert_id:6,events:[{alert_id:5},{alert_id:6}]};await pollReflowAlerts();
assert.equal(plays,1);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'6');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_new_browser_baselines_and_starts_global_five_second_poll(self):
        body = """
let plays=0;playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};initReflowAlertSound();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'4');assert.equal(plays,0);
assert.equal(alertPollDelay,5000);process.stdout.write('ok');})()
        """
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_persisted_cursor_still_baselines_page_before_polling_without_sound(self):
        body = """
let plays=0;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
global.fetch=url=>{fetches.push(url);
  if(url==='/api/reflow/alerts?after=0')return Promise.resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:8,events:[{alert_id:8}]})});
  if(url==='/api/reflow/alerts?after=8')return Promise.resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:8,events:[]})});
  throw new Error('unexpected request '+url);
};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{initReflowAlertSound();await alertPoll();
assert.deepEqual(fetches,['/api/reflow/alerts?after=0','/api/reflow/alerts?after=8']);
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'8');assert.equal(plays,0);process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_failed_page_baseline_is_retried_before_polling(self):
        body = """
let plays=0,baselineAttempts=0;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
global.fetch=url=>{fetches.push(url);
  if(url==='/api/reflow/alerts?after=0'&&++baselineAttempts===1)return Promise.resolve({ok:false,json:()=>Promise.resolve({})});
  if(url==='/api/reflow/alerts?after=0')return Promise.resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:8,events:[{alert_id:8}]})});
  if(url==='/api/reflow/alerts?after=8')return Promise.resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:8,events:[]})});
  throw new Error('unexpected request '+url);
};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{initReflowAlertSound();await new Promise(resolve=>setImmediate(resolve));
await alertPoll();
assert.deepEqual(fetches,['/api/reflow/alerts?after=0','/api/reflow/alerts?after=0','/api/reflow/alerts?after=8']);
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'8');assert.equal(plays,0);process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_disable_enable_invalidates_stale_page_baseline(self):
        body = """
let plays=0,pending=[];storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
activateReflowAudio=()=>Promise.resolve(true);
global.fetch=url=>{fetches.push(url);return new Promise(resolve=>pending.push({url:url,resolve:resolve}));};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
const response=data=>({ok:true,json:()=>Promise.resolve(data)});
(async()=>{initReflowAlertSound();await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(fetches,['/api/reflow/alerts?after=0']);
await setReflowSoundEnabled(false);let enabling=setReflowSoundEnabled(true);
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(fetches,['/api/reflow/alerts?after=0','/api/reflow/alerts?after=0']);
pending[0].resolve(response({latest_alert_id:99,events:[{alert_id:99}]}));
await new Promise(resolve=>setImmediate(resolve));
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');assert.equal(plays,0);
pending[1].resolve(response({latest_alert_id:8,events:[{alert_id:8}]}));await enabling;
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'8');assert.equal(storage[REFLOW_ALERT_SOUND_KEY],'1');
let polling=pollReflowAlerts();await new Promise(resolve=>setImmediate(resolve));
assert.equal(fetches[2],'/api/reflow/alerts?after=8');
pending[2].resolve(response({latest_alert_id:8,events:[]}));await polling;
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'8');assert.equal(plays,0);process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_menu_switch_does_not_stop_global_alert_poll(self):
        body = """
storage[REFLOW_ALERT_CURSOR_KEY]='3';
global.show=()=>{};global.closeSidebar=()=>{};
initReflowAlertSound();selectTab('calculator');
assert.equal(_reflowAlertTimer,9);assert.notEqual(clearedTimer,9);process.stdout.write('ok');
"""
        self.assertEqual(
            run_alert_sound_javascript(body, include_menu_switch=True),
            "ok",
        )

    def test_disabled_sound_does_not_poll_or_advance_cursor(self):
        body = """
(async()=>{storage[REFLOW_ALERT_SOUND_KEY]='0';storage[REFLOW_ALERT_CURSOR_KEY]='3';
fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};await pollReflowAlerts();
assert.equal(fetches.length,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');
process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_overlapping_alert_polls_share_one_request_and_one_playback(self):
        body = """
let resolveFetch,plays=0;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:3,events:[]};await ensureReflowAlertBaseline(0);fetches.length=0;
global.fetch=url=>{fetches.push(url);return new Promise(resolve=>{resolveFetch=()=>resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[{alert_id:4}]})});});};
let first=pollReflowAlerts(),second=pollReflowAlerts();await new Promise(resolve=>setImmediate(resolve));
assert.equal(fetches.length,1);resolveFetch();await Promise.all([first,second]);
assert.equal(plays,1);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'4');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_disabling_during_poll_prevents_playback_and_cursor_advance(self):
        body = """
let resolveFetch,plays=0;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:3,events:[]};await ensureReflowAlertBaseline(0);
global.fetch=()=>new Promise(resolve=>{resolveFetch=()=>resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[{alert_id:4}]})});});
let polling=pollReflowAlerts();await new Promise(resolve=>setImmediate(resolve));
await setReflowSoundEnabled(false);resolveFetch();await polling;
assert.equal(plays,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_failed_enable_does_not_persist_enabled_state_or_change_cursor(self):
        body = """
storage[REFLOW_ALERT_CURSOR_KEY]='3';activateReflowAudio=()=>Promise.resolve(false);
(async()=>{await setReflowSoundEnabled(true);assert.notEqual(storage[REFLOW_ALERT_SOUND_KEY],'1');
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');assert.equal(fetches.length,0);
activateReflowAudio=()=>Promise.resolve(true);global.fetch=()=>Promise.resolve({ok:false,json:()=>Promise.resolve({latest_alert_id:9})});
await setReflowSoundEnabled(true);assert.notEqual(storage[REFLOW_ALERT_SOUND_KEY],'1');
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_slow_new_browser_baseline_finishes_before_alert_poll(self):
        body = """
let resolveBaseline,plays=0;storage[REFLOW_ALERT_SOUND_KEY]='1';
global.fetch=url=>{fetches.push(url);if(fetches.length===1)return new Promise(resolve=>{resolveBaseline=()=>resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[{alert_id:4}]})});});
return Promise.resolve({ok:true,json:()=>Promise.resolve({latest_alert_id:4,events:[]})});};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{initReflowAlertSound();let polling=alertPoll();await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(fetches,['/api/reflow/alerts?after=0']);resolveBaseline();await polling;
assert.deepEqual(fetches,['/api/reflow/alerts?after=0','/api/reflow/alerts?after=4']);
assert.equal(plays,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'4');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_coin_sound_starts_three_ascending_metallic_tones(self):
        body = """
let frequencies=[],starts=[],ramps=[];
class FakeAudioContext{
  constructor(){this.currentTime=10;this.destination={};}
  resume(){return Promise.resolve();}
  createOscillator(){return {type:'',frequency:{setValueAtTime:v=>frequencies.push(v)},
    connect:()=>{},start:v=>starts.push(v),stop:()=>{}};}
  createGain(){return {gain:{setValueAtTime:()=>{},exponentialRampToValueAtTime:(v,t)=>ramps.push([v,t])},
    connect:()=>{}};}
}
window.AudioContext=FakeAudioContext;
(async()=>{assert.equal(await playReflowCoinSound(),true);assert.deepEqual(frequencies,[880,1175,1568]);
assert.equal(starts.length,3);assert.equal(ramps.filter(x=>x[0]===0.0001).length,3);
process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_test_sound_does_not_change_alert_cursor(self):
        body = """
let plays=0;storage[REFLOW_ALERT_CURSOR_KEY]='7';
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{assert.equal(await testReflowCoinSound(),true);assert.equal(plays,1);
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'7');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_test_sound_remains_available_while_formal_alerts_are_disabled(self):
        body = """
let starts=0;storage[REFLOW_ALERT_SOUND_KEY]='0';storage[REFLOW_ALERT_CURSOR_KEY]='7';
class FakeAudioContext{
  constructor(){this.currentTime=10;this.destination={};}resume(){return Promise.resolve();}
  createOscillator(){return {frequency:{setValueAtTime:()=>{}},connect:()=>{},start:()=>{starts++;},stop:()=>{}};}
  createGain(){return {gain:{setValueAtTime:()=>{},exponentialRampToValueAtTime:()=>{}},connect:()=>{}};}
}
window.AudioContext=FakeAudioContext;
(async()=>{assert.equal(await testReflowCoinSound(),true);assert.equal(starts,3);
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'7');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_disable_during_delayed_resume_starts_no_alert_tones(self):
        body = """
let resolveResume,starts=0;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
class DelayedAudioContext{
  constructor(){this.currentTime=10;this.destination={};}
  resume(){return new Promise(resolve=>{resolveResume=resolve;});}
  createOscillator(){return {frequency:{setValueAtTime:()=>{}},connect:()=>{},start:()=>{starts++;},stop:()=>{}};}
  createGain(){return {gain:{setValueAtTime:()=>{},exponentialRampToValueAtTime:()=>{}},connect:()=>{}};}
}
window.AudioContext=DelayedAudioContext;
(async()=>{fetchPayload={latest_alert_id:3,events:[]};await ensureReflowAlertBaseline(0);
fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};
let polling=pollReflowAlerts();await new Promise(resolve=>setImmediate(resolve));
await setReflowSoundEnabled(false);resolveResume();await polling;
assert.equal(starts,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_sound_toggle_markup_has_stable_id(self):
        body = """
assert.ok(reflowSoundControls().includes('id="reflowSoundToggle"'));process.stdout.write('ok');
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_sound_toggle_updates_label_and_click_action_without_rerender(self):
        body = """
let toggle={textContent:'',onclick:null},state={textContent:''},calls=[];
document.getElementById=id=>id==='reflowSoundToggle'?toggle:id==='reflowSoundState'?state:null;
setReflowSoundEnabled=enabled=>{calls.push(enabled);return Promise.resolve();};
(async()=>{storage[REFLOW_ALERT_SOUND_KEY]='1';updateReflowSoundControls();
assert.equal(toggle.textContent,'关闭声音提醒');await toggle.onclick();assert.deepEqual(calls,[false]);
storage[REFLOW_ALERT_SOUND_KEY]='0';updateReflowSoundControls();
assert.equal(toggle.textContent,'开启声音提醒');await toggle.onclick();assert.deepEqual(calls,[false,true]);
process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_blocked_playback_keeps_cursor_and_requests_user_gesture(self):
        body = """
        let state={textContent:''};document.getElementById=id=>id==='reflowSoundState'?state:null;
        class BlockedAudioContext{constructor(){this.currentTime=0;}resume(){return Promise.reject(new Error('blocked'));}}
        window.AudioContext=BlockedAudioContext;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
        (async()=>{fetchPayload={latest_alert_id:3,events:[]};await ensureReflowAlertBaseline(0);
        fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};await pollReflowAlerts();
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');assert.equal(state.textContent,'需要点击恢复声音');
process.stdout.write('ok');})()
"""
        self.assertEqual(run_alert_sound_javascript(body), "ok")

    def test_sidebar_description_and_renderer_are_wired(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("['reflow',", source)
        self.assertIn("['reflow_1h',", source)
        self.assertIn("reflow_1h:", source)
        self.assertIn("function renderMomentumReflow(", source)
        self.assertIn('mode == "reflow"', source)
        self.assertIn("Array.isArray(rows.rows)", source)

    def test_manual_reflow_scan_merges_today_history(self):
        web_ui = importlib.import_module("web_ui")
        scan_result = {
            "rows": [candidate()],
            "scanned": 283,
            "errors": 0,
            "initialized": 0,
        }
        dashboard_payload = {
            "day": "2026-07-30",
            "rows": [candidate()],
        }
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)

        with patch.object(web_ui, "scan_momentum_reflow", return_value=scan_result), \
             patch.object(
                 web_ui, "merge_reflow_signals", return_value=dashboard_payload
             ) as merge:
            response = web_ui.app.test_client().get("/scan/reflow/1h")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["scanning"])
            self.assertTrue(
                wait_until_payload_and_idle(web_ui, dashboard_payload)
            )

        self.assertEqual(web_ui.cache["reflow_1h"], dashboard_payload)
        merge.assert_called_once()

    def test_real_reflow_merge_preserves_scan_counts_and_derives_daily_totals(self):
        web_ui = importlib.import_module("web_ui")
        scan_result = {
            "rows": [candidate()],
            "scanned": 283,
            "errors": 2,
            "initialized": 7,
        }
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(
                    web_ui, "MOMENTUM_REFLOW_HISTORY", root / "history.json"
                ),
                patch.object(
                    web_ui, "MOMENTUM_REFLOW_LEDGER", root / "ledger.json"
                ),
                patch.object(
                    web_ui, "scan_momentum_reflow", return_value=scan_result
                ),
            ):
                payload = web_ui._run_reflow_scan(lambda *_: None)

        self.assertEqual(payload["scanned"], 283)
        self.assertEqual(payload["errors"], 2)
        self.assertEqual(payload["initialized"], 7)
        self.assertEqual(payload["today_total"], 1)
        self.assertEqual(payload["high_quality_count"], 1)

    def test_other_scan_running_blocks_auto_reflow(self):
        web_ui = importlib.import_module("web_ui")
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)
        blocker = Event()
        generic_started = Event()

        def work(progress):
            generic_started.set()
            blocker.wait(1)
            return []

        self.assertTrue(
            web_ui._start_scan_worker("generic", work, lambda result: len(result))
        )
        self.assertTrue(generic_started.wait(1))
        with patch.object(web_ui, "scan_momentum_reflow") as scan:
            self.assertFalse(web_ui._start_reflow_scan("auto"))
        blocker.set()
        self.assertTrue(wait_until_idle(web_ui))
        scan.assert_not_called()

    def test_data_loads_persisted_today_without_market_scan(self):
        web_ui = importlib.import_module("web_ui")
        persisted = {"day": "2026-07-30", "rows": [candidate()]}
        web_ui.cache["reflow_1h"] = None

        with patch.object(
            web_ui, "load_reflow_dashboard", return_value=persisted
        ) as load, patch.object(
            web_ui,
            "load_reflow_settings",
            return_value={"auto_scan_enabled": True},
        ), patch.object(web_ui, "scan_momentum_reflow") as scan:
            payload = web_ui.app.test_client().get("/data").get_json()

        self.assertEqual(payload["data"]["reflow_1h"]["rows"], persisted["rows"])
        self.assertIn("automation", payload["data"]["reflow_1h"])
        load.assert_called_once()
        scan.assert_not_called()

    def test_stale_reflow_worker_cannot_overwrite_payload_or_automation(self):
        web_ui = importlib.import_module("web_ui")
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)
        workers = []

        class DeferredThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target = target
                self.started = False
                workers.append(self)

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        stale_payload = {"day": "2026-07-30", "rows": [candidate()]}
        sentinel_payload = {"day": "2026-07-30", "rows": []}
        with patch.object(web_ui.threading, "Thread", DeferredThread), \
             patch.object(web_ui, "_run_reflow_scan", return_value=stale_payload):
            self.assertTrue(web_ui._start_reflow_scan("auto"))
            stale_worker = workers[-1]
            with web_ui._scan_lock:
                web_ui.state["scanning"] = False
                web_ui._scan_worker = None
            self.assertTrue(web_ui._start_reflow_scan("manual"))

            web_ui.cache["reflow_1h"] = sentinel_payload
            with web_ui._reflow_automation_lock:
                web_ui._reflow_automation["last_auto_scan_at"] = 777
            stale_worker.target()

        self.assertEqual(web_ui.cache["reflow_1h"], sentinel_payload)
        self.assertEqual(web_ui._reflow_automation["last_auto_scan_at"], 777)
        self.assertTrue(web_ui.state["scanning"])

    def test_reflow_admission_reserves_one_worker_before_thread_start(self):
        web_ui = importlib.import_module("web_ui")
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)

        class DeferredThread:
            starts = 0

            def __init__(self, target=None, args=(), daemon=None):
                self.target = target
                self.args = args
                self.started = False

            def start(self):
                self.started = True
                type(self).starts += 1

            def is_alive(self):
                return self.started

        with patch.object(web_ui.threading, "Thread", DeferredThread):
            first = web_ui.app.test_client().get("/scan/reflow/1h")
            second = web_ui.app.test_client().get("/scan/reflow/1h")

        self.assertTrue(first.get_json()["scanning"])
        self.assertTrue(second.get_json()["scanning"])
        self.assertTrue(web_ui.state["scanning"])
        self.assertEqual(DeferredThread.starts, 1)

    def test_generic_scan_blocks_reflow_admission_after_legacy_timeout(self):
        web_ui = importlib.import_module("web_ui")
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)

        class DeferredThread:
            starts = 0

            def __init__(self, target=None, args=(), daemon=None):
                self.started = False

            def start(self):
                self.started = True
                type(self).starts += 1

            def is_alive(self):
                return self.started

        with patch.object(web_ui.threading, "Thread", DeferredThread):
            first = web_ui.app.test_client().get("/scan/breakout/1h")
            web_ui.state["_scan_start"] = time.time() - 121
            second = web_ui.app.test_client().get("/scan/reflow/1h")

        self.assertTrue(first.get_json()["scanning"])
        self.assertTrue(second.get_json()["scanning"])
        self.assertTrue(web_ui.state["scanning"])
        self.assertEqual(DeferredThread.starts, 1)

    def test_funding_scan_blocks_generic_admission(self):
        web_ui = importlib.import_module("web_ui")
        reset_scan_admission(web_ui)
        self.addCleanup(reset_scan_admission, web_ui)

        class DeferredThread:
            starts = 0

            def __init__(self, target=None, args=(), daemon=None):
                self.started = False

            def start(self):
                self.started = True
                type(self).starts += 1

            def is_alive(self):
                return self.started

        with patch.object(web_ui.threading, "Thread", DeferredThread):
            first = web_ui.app.test_client().get("/scan/funding")
            second = web_ui.app.test_client().get("/scan/breakout/1h")

        self.assertTrue(first.get_json()["scanning"])
        self.assertTrue(second.get_json()["scanning"])
        self.assertTrue(web_ui.state["scanning"])
        self.assertEqual(DeferredThread.starts, 1)

    def test_route_rejects_unsupported_reflow_intervals(self):
        web_ui = importlib.import_module("web_ui")
        web_ui.state.update(scanning=False, progress="", text="")

        response = web_ui.app.test_client().get("/scan/reflow/4h")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "reflow only supports 1h")

    def test_renderer_escapes_payload_and_formats_reflow_values(self):
        result = render_reflow_payload({
            "rows": [
                {
                    "symbol": '<img src=x onerror="globalThis.pwned=1">USDT',
                    "direction": "LONG",
                    "price": 12.3456,
                    "ema50": 12.1,
                    "close_distance_atr": 0.18,
                    "window_index": 3,
                    "breakout_time": 0,
                    "max_expansion_atr": 1.25,
                    "daily_kind": '<svg onload="globalThis.pwned=2">',
                    "breakout_volume_ratio": 2.4,
                },
                {
                    "symbol": "SHORTUSDT",
                    "direction": "SHORT",
                    "price": float("nan"),
                    "ema50": float("inf"),
                    "close_distance_atr": None,
                    "window_index": float("inf"),
                    "max_expansion_atr": float("nan"),
                    "daily_kind": "strong_momentum",
                    "breakout_volume_ratio": float("inf"),
                },
            ],
            "scanned": 20,
            "errors": 1,
            "initialized": 3,
        })
        rendered = result["stats"]["innerHTML"] + result["main"]["innerHTML"]

        self.assertIn("LONG", rendered)
        self.assertIn("SHORT", rendered)
        self.assertIn("3/5", rendered)
        self.assertIn("0.18 ATR", rendered)
        self.assertIn("\u5f3a\u52a8\u80fd\u65e5K", rendered)
        self.assertIn("2.4x", rendered)
        self.assertNotIn("1970-01-01 08:00", rendered)
        self.assertIn("reflow-long", rendered)
        self.assertIn("reflow-short", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("&lt;svg", rendered)
        self.assertNotRegex(rendered, r"NaN|Infinity")
        self.assertIn("copySymbol(", rendered)

    def test_renderer_shows_dedicated_empty_state(self):
        result = render_reflow_payload({"rows": [], "scanned": 0, "errors": 0, "initialized": 0})

        self.assertIn("暂无", result["main"]["innerHTML"])
        self.assertIn("首次回流", result["main"]["innerHTML"])

    def test_dashboard_sorts_freshness_and_renders_quality_status_and_type(self):
        old = candidate()
        old.update(
            symbol="OLDUSDT",
            return_open_time=1000,
            quality_score=99,
            quality="HIGH",
            status="ACTIVE",
            instrument_type="COMMODITY",
        )
        new = candidate()
        new.update(
            symbol="NEWUSDT",
            return_open_time=2000,
            quality_score=60,
            quality="STANDARD",
            status="ACTIVE",
            instrument_type="COMMODITY",
        )
        result = render_reflow_payload({
            "rows": [old, new],
            "today_total": 2,
            "high_quality_count": 1,
            "automation": {
                "auto_scan_enabled": True,
                "last_auto_scan_at": 3000,
                "next_scan_at": 4000,
            },
        })
        html = result["stats"]["innerHTML"] + result["main"]["innerHTML"]

        self.assertLess(html.index("NEW"), html.index("OLD"))
        for text in ("高质量", "标准", "商品", "回流有效", "下次扫描"):
            self.assertIn(text, html)

    def test_dashboard_filters_do_not_mutate_source_rows(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn("function setReflowFilter(", source)
        high_long = candidate()
        high_long.update(symbol="HIGHUSDT", quality="HIGH", quality_score=90)
        standard_short = candidate()
        standard_short.update(
            symbol="STANDARDUSDT", direction="SHORT", quality="STANDARD", quality_score=50
        )
        payload = {"rows": [high_long, standard_short]}

        result = render_reflow_filtered_payload(payload, "quality", "HIGH")
        html = result["nodes"]["main"]["innerHTML"]
        self.assertIn("HIGH", html)
        self.assertNotIn("<b>STANDARD</b>", html)
        self.assertEqual(result["sourceLength"], 2)

    def test_mobile_markup_contains_expandable_details(self):
        result = render_reflow_payload({"rows": [candidate()]})

        self.assertIn("reflow-mobile-details", result["main"]["innerHTML"])

    def test_mobile_contract_keeps_freshness_and_details_control_visible(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")

        self.assertIn("reflow-mobile-detail-cell", source)
        self.assertIn("reflow-dashboard table th:nth-child(n+8)", source)

    def test_dashboard_scanning_status_includes_zero_and_missing_progress(self):
        for progress in (0, None):
            result = render_reflow_payload({
                "rows": [],
                "automation": {"scanning": True, "progress": progress},
            })
            stats = result["stats"]["innerHTML"]

            self.assertIn("扫描中", stats)
            if progress == 0:
                self.assertIn("0", stats)

    def test_dashboard_timestamp_zero_or_missing_never_renders_epoch(self):
        row = candidate()
        row.update(breakout_time=0, return_open_time=0)
        result = render_reflow_payload({
            "rows": [row],
            "automation": {"last_auto_scan_at": 0, "next_scan_at": None},
        })
        rendered = result["stats"]["innerHTML"] + result["main"]["innerHTML"]

        self.assertNotIn("1970-01-01", rendered)
        self.assertGreaterEqual(rendered.count("--"), 2)

    def test_reflow_page_polls_and_rerenders_updated_payload(self):
        source = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn("var _reflowPolling=false", source)
        first = candidate()
        first["symbol"] = "FIRSTUSDT"
        updated = candidate()
        updated["symbol"] = "UPDATEDUSDT"

        result = poll_reflow_payload({"rows": [first]}, {"rows": [updated]})

        self.assertEqual(result["interval"], 7)
        self.assertEqual(result["renders"], ["FIRSTUSDT", "UPDATEDUSDT"])


    def test_first_use_prompt_normalizes_mixed_cache_shapes(self):
        self.assertEqual(
            first_use_scan_label({"breakout_1h": [], "reflow_1h": {"rows": []}}),
            "prompt",
        )
        self.assertEqual(
            first_use_scan_label({"breakout_1h": ["BTCUSDT"], "reflow_1h": {"rows": []}}),
            "empty",
        )
        self.assertEqual(
            first_use_scan_label({"breakout_1h": [], "reflow_1h": {"rows": ["BTCUSDT"]}}),
            "empty",
        )

    def test_first_use_prompt_recognizes_nonempty_funding_sides(self):
        for funding in (
            {"negative": ["BTCUSDT"], "positive": []},
            {"negative": [], "positive": ["BTCUSDT"]},
        ):
            self.assertEqual(
                first_use_scan_label({"breakout_1h": [], "funding": funding}),
                "empty",
            )


if __name__ == "__main__":
    unittest.main()
