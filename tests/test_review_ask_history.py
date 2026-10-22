"""Ask AI prompt history (Up/Down recall): pure module run under node + wiring."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dandelion import app

JS = Path(app.__file__).parent / "static" / "js" / "ask_history.js"

HARNESS = r"""
var window = {};
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
function fake(init){
  var d = {}; if(init !== undefined) d['dandelion_askhistory'] = init;
  return {d: d, getItem: function(k){ return k in d ? d[k] : null; },
          setItem: function(k, v){ d[k] = v; }};
}
var out = {};
// push: blanks and consecutive duplicates skipped, cap 200
var s = fake(), h = window.AskHistory(s);
['a', '  ', '', 'a', 'b', 'a'].forEach(function(t){ h.push(t); });
out.pushed = JSON.parse(s.d['dandelion_askhistory']);
s = fake(); h = window.AskHistory(s);
for(var i = 0; i < 205; i++) h.push('p' + i);
var all = JSON.parse(s.d['dandelion_askhistory']);
out.cap = [all.length, all[0], all[all.length - 1]];
// order and draft
s = fake(JSON.stringify(['one', 'two', 'three'])); h = window.AskHistory(s);
out.order = [h.older('draft'), h.older('x'), h.older('x'), h.older('x'),
             h.newer('x'), h.newer('x'), h.newer('x'), h.newer('x')];
out.empty = window.AskHistory(fake()).older('d');
h.older('d2'); h.reset(); out.afterReset = h.newer('z');
// throwing storage
var bad = {getItem: function(){ throw new Error('no'); }, setItem: function(){ throw new Error('no'); }};
h = window.AskHistory(bad); h.push('q'); h.push('r');
out.throwing = [h.older('d'), h.older('d'), h.newer(''), h.newer('')];
out.nostorage = (function(){ var g = window.AskHistory(undefined); g.push('k'); return g.older('d'); })();
out.garbage = window.AskHistory(fake('not json')).older('d');
// immediate(): empty, unedited recalled entry, edited recalled entry
s = fake(JSON.stringify(['one', 'two'])); h = window.AskHistory(s);
var im = [h.immediate(''), h.immediate('typed')];
h.older('typed'); im.push(h.immediate('two'), h.immediate('two!'));
h.newer(''); im.push(h.immediate('typed'));
out.immediate = im;
out.edge = [h.atEdge('x'.repeat(20), 5, 5, true), h.atEdge('x'.repeat(20), 5, 0, true),
            h.atEdge('x'.repeat(20), 5, 20, false), h.atEdge('x'.repeat(20), 5, 2, true),
            h.atEdge('x'.repeat(20), 5, 9, false), h.atEdge('x'.repeat(20), 5, 0, false),
            h.atEdge('abc\ndef', 4, 0, true),            // line 2 col 0, Up
            h.atEdge('abcdef\nxy', 3, 9, false),         // 2nd-to-last line, Down
            h.atEdge('abc\ndef', 2, 0, true),            // first line, Up to start
            h.atEdge('abc\ndef', 5, 7, false)];          // last line, Down to end
// two histories (tabs) sharing one storage keep each other's entries
s = fake(); var A = window.AskHistory(s), B = window.AskHistory(s);
A.push('fromA'); B.push('fromB');
out.tabs = JSON.parse(s.d['dandelion_askhistory']);
// writes throw but reads work: this session's prompts survive
var ro = fake(JSON.stringify(['old']));
ro.setItem = function(){ throw new Error('quota'); };
h = window.AskHistory(ro); h.push('new1'); h.push('new2');
out.readonly = [h.older(''), h.older(''), h.older(''), h.older('')];
// stored size is capped
s = fake(); h = window.AskHistory(s);
for(var j = 0; j < 20; j++) h.push(String(j).repeat(60000));
var capped = JSON.parse(s.d['dandelion_askhistory']);
out.size = [s.d['dandelion_askhistory'].length <= 500000, capped[capped.length - 1][0]];
console.log(JSON.stringify(out));
"""


def test_ask_history_module_under_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", HARNESS, str(JS)], check=False,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["pushed"] == ["a", "b", "a"]
    assert o["cap"] == [200, "p5", "p204"]
    assert o["order"] == ["three", "two", "one", None, "two", "three", "draft", None]
    assert o["empty"] is None
    assert o["afterReset"] is None
    assert o["throwing"] == ["r", "q", "r", "d"]
    assert o["nostorage"] == "k"
    assert o["garbage"] is None
    assert o["edge"] == [True, True, True, False, False, False, False, False, True, True]
    assert o["tabs"] == ["fromA", "fromB"]
    assert o["readonly"] == ["new2", "new1", "old", None]
    assert o["size"] == [True, "1"]
    assert o["immediate"] == [True, False, True, False, False]


def test_composer_wires_arrow_keys_and_module_loads_first():
    js = app.COMPOSER_JS
    assert "ArrowUp" in js and "ArrowDown" in js and "askHistory" in js
    assert "isComposing" in js and "setTimeout" in js and "immediate(" in js and "atEdge(" in js
    assert "229" in js and "arrowSeq" in js
    src = Path(app.__file__).read_text(encoding="utf-8")
    assert src.index('src="/static/js/ask_history.js"') < src.index('src="/static/js/composer.js"')
