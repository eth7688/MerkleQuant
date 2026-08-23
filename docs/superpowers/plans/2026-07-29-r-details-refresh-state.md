# R Review Details Refresh State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the R performance “更多复盘” section in the user-selected open or closed state while the demo dashboard rebuilds every two seconds.

**Architecture:** Store the current `<details>` state in one page-lifetime JavaScript boolean. A small standalone toggle handler updates the boolean, and the shared panel HTML factory emits the `open` attribute when rebuilding the node.

**Tech Stack:** Python 3.12, Flask embedded vanilla JavaScript, Node.js renderer probes, `unittest`.

## Global Constraints

- Modify only the R details state behavior in `web_ui.py` and its focused regression test.
- Keep the two-second polling interval unchanged.
- Do not modify R calculations, API payloads, trading logic, configuration, positions, or records.
- Keep normal and demo dashboards on the same `rPerformancePanelHtml()` implementation.
- Use a standalone toggle function; do not add complex inline JavaScript.
- The state is page-lifetime only and defaults to closed after a full browser reload.

---

### Task 1: Preserve the R details open state across panel rebuilds

**Files:**
- Modify: `tests/test_r_performance_integration.py`
- Modify: `web_ui.py:1710-1725`

**Interfaces:**
- Consumes: the existing `rPerformancePanelHtml() -> string` shared HTML factory.
- Produces: `_rPerformanceDetailsOpen: boolean` and `rememberRPerformanceDetailsState(details) -> undefined`.

- [ ] **Step 1: Write the failing behavioral test**

Add a Node probe and test that execute the actual state functions and HTML factory:

```python
def run_r_details_state_probe():
    source = Path("web_ui.py").read_text(encoding="utf-8")
    start = source.index("var _rPerformanceDetailsOpen")
    end = source.index("function initTraderPanel()")
    script = f"""
{source[start:end]}
var initial=rPerformancePanelHtml();
rememberRPerformanceDetailsState({{open:true}});
var opened=rPerformancePanelHtml();
rememberRPerformanceDetailsState({{open:false}});
var closed=rPerformancePanelHtml();
process.stdout.write(JSON.stringify({{
  initialOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(initial),
  openedOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(opened),
  closedOpen:/<details[^>]*\\sopen(?:\\s|>)/.test(closed),
  hasToggleHandler:opened.indexOf('ontoggle="rememberRPerformanceDetailsState(this)"')>=0
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)
```

```python
def test_details_state_survives_dashboard_html_rebuild(self):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    self.assertIn("var _rPerformanceDetailsOpen", source)
    state = run_r_details_state_probe()
    self.assertFalse(state["initialOpen"])
    self.assertTrue(state["openedOpen"])
    self.assertFalse(state["closedOpen"])
    self.assertTrue(state["hasToggleHandler"])
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest tests.test_r_performance_integration.RPerformanceUiTests.test_details_state_survives_dashboard_html_rebuild -v
```

Expected: `FAIL` because `var _rPerformanceDetailsOpen` is absent.

- [ ] **Step 3: Implement the minimal state preservation**

Immediately before `rPerformancePanelHtml()`, add:

```javascript
var _rPerformanceDetailsOpen=false;
function rememberRPerformanceDetailsState(details){
  _rPerformanceDetailsOpen=!!(details&&details.open);
}
```

Replace the existing details HTML fragment with:

```javascript
+'<details class="r-details" id="rPerformanceDetails"'
+(_rPerformanceDetailsOpen?' open':'')
+' ontoggle="rememberRPerformanceDetailsState(this)"><summary>更多复盘</summary><div class="r-detail-grid" id="rPerformanceDetailGrid"></div></details>'
```

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_r_performance_integration -v
python -m py_compile web_ui.py
python -m unittest discover -s tests
git diff --check
```

Expected: the new state test passes, all existing tests pass, compilation exits `0`, and `git diff --check` prints no errors.

- [ ] **Step 5: Commit the tested fix**

```powershell
git add web_ui.py tests/test_r_performance_integration.py
git commit -m "fix: preserve R review details state"
```

### Task 2: Deploy the isolated UI fix to the demo engine

**Files:**
- Modify after successful deployment: `PROGRESS.md`
- Deploy: `web_ui.py`

**Interfaces:**
- Consumes: the verified `web_ui.py` from Task 1.
- Produces: the same R panel behavior on the server with no API or trading-engine changes.

- [ ] **Step 1: Capture immutable pre-deploy evidence**

Record local/server SHA256 for `web_ui.py`, confirm both services are active, and query:

```text
running
positions count and symbols
trade_count
r_performance.status
```

Expected: demo engine is running and the live counts are recorded before deployment.

- [ ] **Step 2: Stage, compile, and back up**

Set `stamp=$(date +%Y%m%d_%H%M%S)` and upload only `web_ui.py` to `/tmp/axiom_r_details_$stamp/`. Run:

```bash
python3 -m py_compile /tmp/axiom_r_details_$stamp/web_ui.py
tar czf /root/axiom_r_details_$stamp.tar.gz <deploy-dir>/web_ui.py
```

Expected: compilation succeeds and the timestamped backup exists.

- [ ] **Step 3: Install and restart only the web service**

Run:

```bash
install -m 0644 /tmp/axiom_r_details_$stamp/web_ui.py <deploy-dir>/web_ui.py
python3 -m py_compile <deploy-dir>/web_ui.py
systemctl restart macd-bot
```

Do not restart `macd-admin`.

- [ ] **Step 4: Verify deployed behavior and data preservation**

Verify:

```text
macd-bot=active
macd-admin=active
server web_ui.py SHA256 equals local SHA256
/demo/status?fast=1 returns running=true
positions and trade_count match the pre-deploy snapshot
r_performance.status=ok
recent service logs contain no Traceback, ImportError, SyntaxError, or ModuleNotFoundError
```

Use a browser or real DOM automation to expand “更多复盘” and observe that it remains open across at least two polling cycles.

- [ ] **Step 5: Record and commit deployment evidence**

Append to `PROGRESS.md`:

```text
root cause and minimal UI change
deployed file
server backup path
local/server SHA256
test count
pre/post positions and trade_count
browser polling verification
service/log verification
```

Then run:

```powershell
git add PROGRESS.md
git commit -m "docs: record R details state deployment"
```
