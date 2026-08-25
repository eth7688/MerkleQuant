# 动能压缩交易对复制按钮实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在动能压缩页面的 LONG/SHORT 观察池、当前突破和终止结构中，为交易对增加可复制完整 symbol 的现有样式按钮。

**Architecture:** 在现有动能压缩前端渲染区增加一个纯字符串辅助函数，统一生成安全转义的复制按钮和交易对文字。三个表格渲染路径复用该函数，不改变 API、数据结构、表格列数或现有 `copySymbol()` 行为。

**Tech Stack:** Python 3.12、Flask 内嵌原生 HTML/CSS/JavaScript、Node.js 渲染测试、`unittest`。

## Global Constraints

- 仅修改 `web_ui.py` 和 `tests/test_momentum_compression_integration.py`。
- 复用 `.copy-sym` 与 `copySymbol()`，不新增 CSS、前端依赖或复制列。
- 复制原始完整 `symbol`；页面文字继续显示完整交易对。
- `data-symbol` 和可见文字都必须经 `escapeRHtml()` 转义。
- 不修改输入 payload，不改变现有 DOM ID、列数、刷新、声音、微信、扫描、交易或权限逻辑。
- 不部署服务器，除非实现完成后用户再次明确确认。

---

### Task 1: 为全部动能压缩交易对单元格增加复制按钮

**Files:**
- Modify: `web_ui.py:1973-2030`
- Test: `tests/test_momentum_compression_integration.py:490-555`

**Interfaces:**
- Consumes: existing `escapeRHtml(value) -> string` and `copySymbol(symbol, element)`.
- Produces: `compressionSymbolHtml(row: object) -> string`, returning one escaped copy icon plus escaped full-symbol label.

- [ ] **Step 1: Write the failing renderer test**

Extend the existing full-symbol renderer test so each target section must contain exactly one button for its row, and add an escaping case:

```python
def test_renderer_adds_safe_copy_buttons_to_each_compression_table(self):
    payload = {
        "pool_rows": [
            {"symbol": "POOLONLYUSDT", "side": "LONG", "state": "PRE_BREAKOUT"},
            {"symbol": "CURRENTONLYUSDT", "side": "SHORT", "state": "BREAKOUT_ACTIVE_SHORT"},
        ],
        "episode_rows": [
            {"symbol": "TERMINALONLYUSDT", "side": "SHORT", "state": "BREAKOUT_FAILED"},
        ],
    }
    result = render_compression_payload_twice(payload)
    rendered = result["first"]["main"]

    pool_section = rendered[rendered.index("LONG 观察池"):rendered.index("SHORT 观察池")]
    current_section = rendered[rendered.index("当前突破"):rendered.index("终止结构")]
    terminal_section = rendered[rendered.index("终止结构"):rendered.index("拒绝统计")]
    self.assertIn('data-symbol="POOLONLYUSDT"', pool_section)
    self.assertIn('data-symbol="CURRENTONLYUSDT"', current_section)
    self.assertIn('data-symbol="TERMINALONLYUSDT"', terminal_section)
    self.assertEqual(result["before"], result["after"])
    self.assertEqual(result["first"], result["second"])

def test_renderer_escapes_copy_symbol_attribute_and_label(self):
    rendered = render_compression_payload({
        "pool_rows": [{
            "symbol": 'BAD\"<TAG>&\'USDT',
            "side": "LONG",
            "state": "PRE_BREAKOUT",
        }],
    })["main"]["innerHTML"]
    self.assertIn('data-symbol="BAD&quot;&lt;TAG&gt;&amp;&#39;USDT"', rendered)
    self.assertNotIn('data-symbol="BAD\"<TAG>', rendered)
    self.assertIn('onclick="event.stopPropagation();copySymbol(this.getAttribute(\'data-symbol\'),this)"', rendered)
```

- [ ] **Step 2: Run RED verification**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration.CompressionDashboardUiTests.test_renderer_adds_safe_copy_buttons_to_each_compression_table tests.test_momentum_compression_integration.CompressionDashboardUiTests.test_renderer_escapes_copy_symbol_attribute_and_label -q
```

Expected: both tests fail because the compression tables currently render plain `<b>symbol</b>` cells without `data-symbol` copy buttons.

- [ ] **Step 3: Add one safe reusable symbol-cell renderer**

Add beside the existing compression renderer helpers:

```javascript
function compressionSymbolHtml(row){
  row=row&&typeof row==='object'?row:{};
  var symbol=String(row.symbol===null||row.symbol===undefined?'':row.symbol);
  var safe=escapeRHtml(symbol);
  return '<span class="copy-sym" data-symbol="'+safe+'" onclick="event.stopPropagation();copySymbol(this.getAttribute(\'data-symbol\'),this)" title="复制"></span> <b>'+(safe||'--')+'</b>';
}
```

Use `compressionSymbolHtml(row)` in exactly these paths:

```javascript
// compressionRowHtml(): LONG/SHORT observation tables
'<td>'+compressionSymbolHtml(row)+'</td>'

// current breakout rows
'<td>'+compressionSymbolHtml(row)+'</td>'

// terminal rows
'<td>'+compressionSymbolHtml(row)+'</td>'
```

Keep all existing row filters, sorting, columns and formatting unchanged.

- [ ] **Step 4: Run focused GREEN verification**

Run:

```powershell
python -m unittest tests.test_momentum_compression_integration -q
```

Expected: all compression integration tests pass, including safe attributes, four target areas and render purity.

- [ ] **Step 5: Run full verification**

Run:

```powershell
python -m unittest discover -s tests -q
python -m py_compile web_ui.py
git diff --check
```

Expected: zero failures/errors; compilation and diff check exit `0`. Only the two existing untracked runtime lock files may remain outside the diff.

- [ ] **Step 6: Review and commit**

Review the complete diff against `docs/superpowers/specs/2026-08-25-compression-symbol-copy-design.md`, then commit only the runtime file and focused test:

```powershell
git add web_ui.py tests/test_momentum_compression_integration.py
git commit -m "feat: add compression symbol copy buttons"
```

Stop after reporting the commit and fresh verification evidence. Production deployment requires a separate explicit confirmation and a new server backup.
