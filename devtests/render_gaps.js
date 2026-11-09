#!/usr/bin/env node
/* 断流清单前端的渲染自检（不装 jsdom，手搭最小桩）。
 *
 * 从 index.html 里把 gapSev / gapFilter / gapList 三个函数原样抠出来（抠不到就直接
 * 报错，这样改了函数名或挪了位置也会被这条测试发现），喂真实 fixture 渲染一遍，检查：
 *   · chips 数量与计数；gaprow 数量 == 后端给的条数；行上带 data-bucket
 *   · 48 小时窗口文案 + 口径提示都渲染出来
 *   · div 开闭平衡（手拼 HTML 最容易在这里出问题）
 *   · 档位筛选把不匹配的行设成 display:none、只照亮当前按钮、空档位给提示
 */
const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'erm', 'app', 'www', 'index.html');
const FIX = '/tmp/fixture_gaps.json';
const src = fs.readFileSync(HTML, 'utf8');
const data = JSON.parse(fs.readFileSync(FIX, 'utf8'));

const FAILS = [];
function check(name, ok, detail) {
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (detail ? '   ' + detail : ''));
  if (!ok) FAILS.push(name);
}

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('没找到 function ' + name);
  const end = src.indexOf('\n}', i);
  if (end < 0) throw new Error('function ' + name + ' 没找到顶格结尾');
  return src.slice(i, end + 2);
}

const body = "let GAP_FILTER = 'all';\n" +
  ['gapSev', 'gapFilter', 'gapList'].map(grab).join('\n\n');

/* ---- 最小 DOM 桩 ---- */
const rows = data.gaps.map(g => ({ dataset: { bucket: g.bucket }, style: {} }));
const btns = data.gap_buckets.map(b => ({
  dataset: { key: b.key },
  classList: { on: b.key === 'all', toggle(k, v) { this.on = v; } },
}));
const emptyEl = { style: { display: 'none' } };
const box = {
  _rows: rows,
  querySelectorAll(sel) { return sel === '.gaprow' ? this._rows : btns; },
  querySelector(sel) { return sel === '.gempty' ? emptyEl : null; },
};

const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const api = new Function('$', 'esc', body +
  '\n;return {gapList, gapFilter, gapSev, gf: () => GAP_FILTER};')(
  () => box, esc);

/* ---- 1. 渲染 ---- */
const html = api.gapList(data);
const count = re => (html.match(re) || []).length;

check('返回一段 HTML', typeof html === 'string' && html.length > 0, html.length + ' 字符');
check('外层是 .gapwrap', html.startsWith('<div class="gapwrap">'));
check('div 开闭平衡', count(/<div\b/g) === count(/<\/div>/g),
      count(/<div\b/g) + ' vs ' + count(/<\/div>/g));
check('渲染出「最近 48 小时」', html.includes('最近 48 小时'));
check('渲染出窗口起止时间',
      html.includes(data.gap_window.start_str.slice(5, 16)) &&
      html.includes(data.gap_window.end_str.slice(5, 16)));
check('渲染出口径提示', html.includes('间隔超过 5 分钟才算一段'));
check('chips 数量 = 1 + 档位数', count(/<button\b/g) === data.gap_buckets.length,
      count(/<button\b/g) + ' vs ' + data.gap_buckets.length);
check('chips 首项是「全部」且计数 = gap_total',
      html.includes('data-key="all"') &&
      html.includes('全部<span class="c">' + data.gap_total));
check('gaprow 数量 = 后端条数', count(/class="gaprow"/g) === data.gaps.length,
      count(/class="gaprow"/g) + ' vs ' + data.gaps.length);
check('每行都带 data-bucket', count(/data-bucket="/g) === data.gaps.length);
/* 按行切开逐条比对：顺序 + 徽标文案（转义后）。只做全局 includes 会被
   "> 1 小时" 这种文案骗过 —— esc() 会把 > 转成 &gt;，裸文本永远匹配不上。 */
const rowHtml = html.split('<div class="gaprow"').slice(1);
check('每行都显示档位徽标',
      rowHtml.length === data.gaps.length &&
      rowHtml.every((r, i) => r.includes('class="gsev"') &&
                              r.includes(esc(data.gaps[i].bucket_label))),
      rowHtml.length + ' 行');
check('每行都显示通道名与时间',
      data.gaps.every(g => html.includes(g.label) && html.includes(g.from_str)));
check('空档位提示文案存在', html.includes('这个档位下没有记录'));
check('整窗无录像那段（CAM_B）被渲染',
      data.gaps.some(g => g.channel === 'CAM_B') === html.includes('CAM_B'),
      'fixture 有 ' + data.gaps.filter(g => g.channel === 'CAM_B').length + ' 条');
check('从没录过的通道（CAM_C）不出现', !html.includes('CAM_C'));

/* ---- 2. 档位筛选 ---- */
api.gapFilter({ dataset: { key: 'gt60' } });
check('筛「> 1 小时」后 GAP_FILTER 被设置', api.gf() === 'gt60', api.gf());
check('不匹配的行被隐藏',
      rows.every(r => r.style.display === 'none' || r.dataset.bucket === 'gt60'));
check('只有 gt60 按钮亮起',
      btns.filter(b => b.classList.on).map(b => b.dataset.key).join(',') === 'gt60');
check('有匹配时提示保持隐藏', emptyEl.style.display === 'none');
check('gapSev 给四档都配了颜色',
      ['le10', 'm10_30', 'm30_60', 'gt60'].every(k => {
        const v = api.gapSev(k);
        return Array.isArray(v) && v.length === 2 && v[0] && v[1];
      }));

box._rows = rows.map(() => ({ dataset: { bucket: 'le10' }, style: {} }));
btns.forEach(b => { b.classList.on = false; });
api.gapFilter({ dataset: { key: 'gt60' } });
check('一条都不匹配时提示转为可见', emptyEl.style.display !== 'none', emptyEl.style.display);

console.log();
if (FAILS.length) {
  console.log('FAILED: ' + FAILS.length + ' 项 —— ' + FAILS.join('; '));
  process.exit(1);
}
console.log('ALL PASS');
