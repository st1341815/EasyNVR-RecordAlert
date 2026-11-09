#!/usr/bin/env node
/* 断流清单前端的渲染自检（不装 jsdom，手搭最小桩）。
 *
 * 从 index.html 里把 gapSev / gapFilter / applyGapView / gapToggle / gapList 原样抠
 * 出来（抠不到就直接报错，这样改了函数名或挪了位置也会被这条测试发现），喂真实
 * fixture 渲染一遍，检查：
 *   · chips 数量与计数；gaprow 数量 == 后端给的条数；行上带 data-bucket
 *   · 48 小时窗口文案 + 口径提示都渲染出来
 *   · div 开闭平衡（手拼 HTML 最容易在这里出问题）
 *   · 档位筛选把不匹配的行设成 display:none、只照亮当前按钮、空档位给提示
 *   · 超过 GAP_MAX_ROWS 行自动折叠、展开/收起、换档位回到折叠态
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

/* 折叠行数是需求里写死的数（50），从源码里抠出来，改了就会被断言抓到 */
const maxM = src.match(/const GAP_MAX_ROWS = (\d+);/);
if (!maxM) throw new Error('没找到 const GAP_MAX_ROWS');
const GAP_MAX_ROWS = Number(maxM[1]);

const body = "let GAP_FILTER = 'all';\nlet GAP_EXPAND = false;\n" +
  'const GAP_MAX_ROWS = ' + GAP_MAX_ROWS + ';\n' +
  ['gapSev', 'gapFilter', 'applyGapView', 'gapToggle', 'gapList'].map(grab).join('\n\n');

/* ---- 最小 DOM 桩 ---- */
const btns = data.gap_buckets.map(b => ({
  dataset: { key: b.key },
  classList: { on: b.key === 'all', toggle(k, v) { this.on = v; } },
}));
const emptyEl = { style: { display: 'none' } };
let rows = data.gaps.map(g => ({ dataset: { bucket: g.bucket }, style: {} }));

function mkMore() {
  const txt = { textContent: '' };
  const btn = { textContent: '', clicked: 0 };
  const el = {
    style: { display: 'none' },
    querySelector(sel) {
      if (sel === '.gmore-txt') return txt;
      if (sel === 'button') return btn;
      return null;
    },
  };
  return { el: el, txt: txt, btn: btn };
}
const more = mkMore();

const box = {
  _rows: rows,
  querySelectorAll(sel) { return sel === '.gaprow' ? this._rows : btns; },
  querySelector(sel) {
    if (sel === '.gempty') return emptyEl;
    if (sel === '.gmore') return more.el;
    return null;
  },
};

const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const api = new Function('$', 'esc', body +
  '\n;return {gapList, gapFilter, applyGapView, gapToggle, gapSev, ' +
  'gf: () => GAP_FILTER, sf: v => { GAP_FILTER = v; }, ' +
  'ge: () => GAP_EXPAND, se: v => { GAP_EXPAND = v; }};')(
  () => box, esc);

const visible = () => rows.filter(r => r.style.display !== 'none').length;
const hidden = () => rows.filter(r => r.style.display === 'none').length;

/* ---- 1. 渲染 ---- */
const html = api.gapList(data);
const count = re => (html.match(re) || []).length;

check('折叠阈值 = 50 行', GAP_MAX_ROWS === 50, String(GAP_MAX_ROWS));
check('返回一段 HTML', typeof html === 'string' && html.length > 0, html.length + ' 字符');
check('外层是 .gapwrap', html.startsWith('<div class="gapwrap">'));
check('div 开闭平衡', count(/<div\b/g) === count(/<\/div>/g),
      count(/<div\b/g) + ' vs ' + count(/<\/div>/g));
check('渲染出「最近 48 小时」', html.includes('最近 48 小时'));
check('渲染出窗口起止时间',
      html.includes(data.gap_window.start_str.slice(5, 16)) &&
      html.includes(data.gap_window.end_str.slice(5, 16)));
check('渲染出口径提示', html.includes('间隔超过 5 分钟才算一段'));
/* 展开按钮也是 <button>，所以按 onclick 认 chips，别数总按钮数 */
check('chips 数量 = 1 + 档位数',
      count(/onclick="gapFilter\(this\)"/g) === data.gap_buckets.length,
      count(/onclick="gapFilter\(this\)"/g) + ' vs ' + data.gap_buckets.length);
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
check('带折叠条容器（默认隐藏）',
      html.includes('class="gmore"') && html.includes('onclick="gapToggle()"'));
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

box._rows = rows = data.gaps.map(() => ({ dataset: { bucket: 'le10' }, style: {} }));
btns.forEach(b => { b.classList.on = false; });
api.gapFilter({ dataset: { key: 'gt60' } });
check('一条都不匹配时提示转为可见', emptyEl.style.display !== 'none', emptyEl.style.display);

/* ---- 3. 超过 50 行自动折叠 ---- */
function setRows(n, bucketAt) {
  rows = Array.from({ length: n }, (_, i) => ({
    dataset: { bucket: bucketAt ? bucketAt(i) : 'gt60' }, style: {},
  }));
  box._rows = rows;
  api.sf('all'); api.se(false);
  api.applyGapView();
}

setRows(50);
check('正好 50 行 → 全显示', visible() === 50, String(visible()));
check('正好 50 行 → 折叠条隐藏', more.el.style.display === 'none', more.el.style.display);

setRows(51);
check('51 行 → 收在 50', visible() === 50, String(visible()));
check('51 行 → 折叠条出现', more.el.style.display === '');
check('51 行 → 文案', more.txt.textContent === '已显示前 50 段，1 段已折叠',
      more.txt.textContent);
check('51 行 → 按钮', more.btn.textContent === '展开剩余 1 段', more.btn.textContent);

setRows(200, i => (i % 2 ? 'gt60' : 'le10'));
check('200 行（两档各 100）→ 只铺 50 行', visible() === 50, String(visible()));
check('200 行 → 折叠 150 行', hidden() === 150, String(hidden()));
check('200 行 → 文案', more.txt.textContent === '已显示前 50 段，150 段已折叠',
      more.txt.textContent);
check('200 行 → 按钮', more.btn.textContent === '展开剩余 150 段', more.btn.textContent);

api.gapToggle();
check('展开后 200 行全显示', visible() === 200, String(visible()));
check('展开后文案', more.txt.textContent === '已展开全部 200 段', more.txt.textContent);
check('展开后按钮变「收起」', more.btn.textContent === '收起', more.btn.textContent);

api.gapToggle();
check('再收起回到 50 行', visible() === 50, String(visible()));
check('再收起按钮复原', more.btn.textContent === '展开剩余 150 段', more.btn.textContent);

/* 15 秒轮询会重建 DOM：展开态要能延续（applyGapView 在 gapList 之后被调用） */
api.se(true);
api.applyGapView();
check('轮询重渲染后保持展开', visible() === 200, String(visible()));

/* 换档位：行集合变了，视角回到折叠态，免得「看着全是大于1小时」其实只是筛掉了 */
api.gapFilter({ dataset: { key: 'gt60' } });
check('换档位后重置为折叠态（该档 100 行 → 显示 50）',
      api.ge() === false && visible() === 50, 'expand=' + api.ge() + ' vis=' + visible());
check('换档位后折叠条按新档位计数',
      more.txt.textContent === '已显示前 50 段，50 段已折叠', more.txt.textContent);

api.gapFilter({ dataset: { key: 'm30_60' } });
check('换到只有 0 条的档位 → 全隐藏、无折叠条',
      visible() === 0 && more.el.style.display === 'none',
      visible() + ' / ' + more.el.style.display);

api.gapFilter({ dataset: { key: 'all' } });
check('回到「全部」→ 重新折叠到 50', visible() === 50, String(visible()));

console.log();
if (FAILS.length) {
  console.log('FAILED: ' + FAILS.length + ' 项 —— ' + FAILS.join('; '));
  process.exit(1);
}
console.log('ALL PASS');
