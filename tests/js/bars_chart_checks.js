// 봉·지표 차트 순수 로직 검증 — bars_chart.js에서 함수를 추출해 실제 실행한다.
// 실행: node tests\js\bars_chart_checks.js
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'bars_chart.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  const open = src.indexOf('{', start);
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`${name} unbalanced`);
}

// 상수는 소스에서 읽는다. 여기 값을 따로 적어두면 bars_chart.js가 값을 바꿔도
// 테스트는 옛 값으로 계속 통과한다 — 둘이 소리 없이 어긋난다 (스펙 §17.3).
function constValue(name) {
  const marker = 'const ' + name + ' = ';
  const at = src.indexOf(marker);
  if (at < 0) throw new Error('const ' + name + ' not found in bars_chart.js');
  const end = src.indexOf(';', at + marker.length);
  return eval(src.slice(at + marker.length, end));
}

const BARS_MIN_CANDLE_PX = constValue('BARS_MIN_CANDLE_PX');
const BARS_DOMAIN_PAD = constValue('BARS_DOMAIN_PAD');

eval(extract('barsPriceDomain'));
eval(extract('barsMacdDomain'));
eval(extract('barsTimeIndex'));
eval(extract('barsCandleWidth'));
eval(extract('barsYAt'));
eval(extract('barsVolumeDomain'));
eval(extract('barsIndexAtX'));
eval(extract('barsTimeTicks'));
eval(extract('barsGapX'));
eval(extract('barsWindow'));
eval(extract('barsZoomView'));
eval(extract('barsSliceView'));

let failures = 0;
const check = (label, cond) => {
  if (!cond) { console.error(`FAIL ${label}`); failures++; }
  else console.log(`ok   ${label}`);
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

const BARS = [
  {high: 110, low: 90, open: 95, close: 105},
  {high: 130, low: 100, open: 105, close: 125},
  {high: 120, low: 80, open: 125, close: 85},
];

// 가격 도메인은 고가·저가를 모두 담고 이동평균선도 담는다
{
  const d = barsPriceDomain(BARS, [null, null, 150]);
  check('price domain covers every low', d.min <= 80);
  check('price domain covers the sma above every high', d.max >= 150);
}

// 이동평균이 전부 null이어도 도메인이 성립한다
{
  const d = barsPriceDomain(BARS, [null, null, null]);
  check('null sma does not poison the domain', d.min <= 80 && d.max >= 130);
}

// 봉이 없으면 도메인이 무너지지 않는다
{
  const d = barsPriceDomain([], []);
  check('empty bars give a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
  check('empty domain is not inverted', d.max > d.min);
}

// 모든 값이 같아도 도메인이 0폭이 되지 않는다 (0으로 나누기 방지)
{
  const flat = [{high: 100, low: 100, open: 100, close: 100}];
  const d = barsPriceDomain(flat, [100]);
  check('flat bars get a padded domain', d.max > d.min);
}

// MACD 도메인은 0을 반드시 포함한다 — 0 기준선이 화면 밖으로 나가면 안 된다
{
  const d = barsMacdDomain([{macd: 5, signal: 4, hist: 1}, {macd: 8, signal: 6, hist: 2}]);
  check('macd domain includes zero', d.min <= 0 && d.max >= 8);
}
{
  const d = barsMacdDomain([{macd: -5, signal: -4, hist: -1}]);
  check('negative macd domain still includes zero', d.min <= -5 && d.max >= 0);
}
{
  const d = barsMacdDomain([{macd: null, signal: null, hist: null}]);
  check('all-null macd gives a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
}

// x 좌표는 봉 중심이고 좌에서 우로 단조 증가한다
{
  const xs = [0, 1, 2].map(i => barsTimeIndex(BARS, i, 300, 40));
  check('x is monotonically increasing', xs[0] < xs[1] && xs[1] < xs[2]);
  check('first bar sits inside the chart area', xs[0] > 40);
  check('last bar stays inside the chart area', xs[2] < 340);
  check('empty bars do not divide by zero', Number.isFinite(barsTimeIndex([], 0, 300, 40)));
}

// 캔들 폭은 양수이고 봉이 많아질수록 좁아진다
{
  const wide = barsCandleWidth(10, 300);
  const narrow = barsCandleWidth(200, 300);
  check('candle width is positive', wide > 0 && narrow > 0);
  check('more bars means narrower candles', narrow < wide);
  check('candle width never collapses to zero', barsCandleWidth(5000, 300) >= 1);
  check('zero bars still give a positive candle width', Number.isFinite(barsCandleWidth(0, 300)) && barsCandleWidth(0, 300) >= 1);
}

// y 매핑은 뒤집혀 있다 — 큰 값이 위(작은 y)
{
  const domain = {min: 0, max: 100};
  check('max maps to the top', near(barsYAt(100, domain, 10, 200), 10));
  check('min maps to the bottom', near(barsYAt(0, domain, 10, 200), 210));
  check('mid maps to the middle', near(barsYAt(50, domain, 10, 200), 110));
}

// 0폭 도메인이 들어와도 NaN을 내지 않는다
{
  const y = barsYAt(5, {min: 5, max: 5}, 10, 200);
  check('zero-width domain does not produce NaN', Number.isFinite(y));
}

// 거래량 축은 0에서 시작한다 — 밑을 자르면 막대 길이 비교가 거짓말이 된다
{
  const d = barsVolumeDomain([{volume: 10}, {volume: 40}, {volume: 25}]);
  check('volume domain starts at zero', d.min === 0);
  check('volume domain covers the tallest bar', d.max >= 40);
  const empty = barsVolumeDomain([]);
  check('empty volume domain is finite', Number.isFinite(empty.max) && empty.max > empty.min);
  const missing = barsVolumeDomain([{volume: null}, {}]);
  check('missing volume does not invert the domain', missing.max > missing.min);
}

// x → 봉 인덱스는 barsTimeIndex의 역함수여야 한다 (십자선이 짚는 봉)
{
  const rows = [{}, {}, {}, {}, {}];
  let roundTrip = true;
  for (let i = 0; i < rows.length; i++) {
    const x = barsTimeIndex(rows, i, 500, 50);
    if (barsIndexAtX(x, 500, 50, rows.length) !== i) roundTrip = false;
  }
  check('index at x round-trips through barsTimeIndex', roundTrip);
  check('left of the chart clamps to the first bar', barsIndexAtX(0, 500, 50, 5) === 0);
  check('right of the chart clamps to the last bar', barsIndexAtX(9999, 500, 50, 5) === 4);
  check('zero width does not produce NaN', Number.isFinite(barsIndexAtX(10, 0, 50, 5)));
  check('no bars still gives a valid index', barsIndexAtX(100, 500, 50, 0) === 0);
}

// 시간 눈금은 겹치지 않을 간격만 고르고, 봉이 빠져도 어긋나지 않는다
{
  const day = [];
  for (let m = 0; m < 360; m++) {
    const hh = String(9 + Math.floor(m / 60)).padStart(2, '0');
    const mm = String(m % 60).padStart(2, '0');
    day.push({time: hh + mm + '00'});
  }
  const wide = barsTimeTicks(day, 1400, 56);
  check('tight chart does not label every minute', wide.length < 30);
  check('tight chart still labels something', wide.length > 1);
  const gapPx = (1400 / day.length) * (wide[1].index - wide[0].index);
  check('labels are at least the minimum gap apart', gapPx >= 56);
  check('labels read as HH:MM', /^\d\d:\d\d$/.test(wide[0].label));
  check('label matches its own bar time',
    wide[0].label.replace(':', '') === day[wide[0].index].time.slice(0, 4));

  // 봉이 빠진 구간이 있어도 라벨은 그 봉의 실제 시각을 따른다
  const holes = [{time: '090000'}, {time: '091500'}, {time: '093000'}, {time: '100000'}];
  const t = barsTimeTicks(holes, 600, 56);
  check('gapped series labels every surviving round minute', t.length === 4);
  check('gapped series keeps time and index aligned',
    t.every(x => x.label.replace(':', '') === holes[x.index].time.slice(0, 4)));

  check('no bars gives no ticks', barsTimeTicks([], 600, 56).length === 0);
  check('zero width gives no ticks', barsTimeTicks(holes, 0, 56).length === 0);
  check('malformed time is skipped, not guessed',
    barsTimeTicks([{time: 'zzzz'}, {time: '100000'}], 600, 56).length === 1);
}

// 갭 경계선은 두 캔들 정확히 사이에 선다
{
  const rows = [{}, {}, {}, {}, {}];
  const before = barsTimeIndex(rows, 2, 500, 50);
  const after = barsTimeIndex(rows, 3, 500, 50);
  const x = barsGapX(rows, 3, 500, 50);
  check('gap boundary sits between the two candles', x > before && x < after);
  check('gap boundary is exactly halfway', near(x, (before + after) / 2));
  check('a gap at the first bar clamps inside the chart', barsGapX(rows, 0, 500, 50) === 50);
  check('an out-of-range index does not escape the chart',
    barsGapX(rows, 99, 500, 50) <= 50 + 500);
  check('a missing index does not produce NaN',
    Number.isFinite(barsGapX(rows, undefined, 500, 50)));
  check('no bars does not divide by zero', Number.isFinite(barsGapX([], 1, 500, 50)));
}

// 표시 창 — 증권사 차트처럼 일부만 띄운다
{
  check('the window shows the tail by default',
    JSON.stringify(barsWindow(374, {count: 120, end: null})) ===
    JSON.stringify({start: 254, end: 374, count: 120}));
  check('a window wider than the data collapses to the data',
    JSON.stringify(barsWindow(30, {count: 120, end: null})) ===
    JSON.stringify({start: 0, end: 30, count: 30}));
  check('no bars gives an empty window',
    JSON.stringify(barsWindow(0, {count: 120, end: null})) ===
    JSON.stringify({start: 0, end: 0, count: 0}));
  const panned = barsWindow(374, {count: 100, end: 200});
  check('a panned window keeps its width', panned.count === 100);
  check('a panned window sits where it was told', panned.start === 100 && panned.end === 200);
  check('panning past the left edge clamps',
    barsWindow(374, {count: 100, end: 3}).start === 0);
  check('panning past the right edge clamps',
    barsWindow(374, {count: 100, end: 9999}).end === 374);
  check('a garbage view still yields a usable window',
    Number.isFinite(barsWindow(374, {count: NaN, end: NaN}).count));
}

// 줌 — 커서 아래 봉이 제자리에 남는다
{
  const total = 374;
  const before = {count: 120, end: 300};
  const anchor = 250;
  const zoomed = barsZoomView(before, total, 0.5, anchor);
  check('zooming in narrows the window', zoomed.count === 60);
  const win = barsWindow(total, zoomed);
  check('the anchored bar stays inside the window',
    anchor >= win.start && anchor < win.end);
  const beforeWin = barsWindow(total, before);
  const ratioBefore = (anchor - beforeWin.start) / (beforeWin.count - 1);
  const ratioAfter = (anchor - win.start) / (win.count - 1);
  check('the anchored bar keeps its position on screen',
    Math.abs(ratioBefore - ratioAfter) < 0.05);
  check('zooming out past the data clamps to the data',
    barsZoomView({count: 300, end: null}, total, 4, 300).count === total);
  check('zooming in stops at the minimum window',
    barsZoomView({count: 22, end: null}, total, 0.1, 370).count === 20);
  check('zooming at the right edge keeps following the latest',
    barsZoomView({count: 120, end: null}, total, 0.5, 373).end === null);
}

// 창 자르기 — 지표와 갭이 봉과 같이 잘린다
{
  const payload = {
    bars: [{time: '090000'}, {time: '090100'}, {time: '090500'}, {time: '090600'}],
    indicators: {
      ma: {'5': [1, 2, 3, 4], '20': [null, null, 5, 6]},
      vol_ma: {'5': [7, 8, 9, 10]},
      macd: [{macd: 1}, {macd: 2}, {macd: 3}, {macd: 4}],
    },
    meta: {gaps: [{after: '090100', resume: '090500', index: 2, missing: 3, jump_pct: -1}]},
  };
  const view = barsSliceView(payload, {start: 1, end: 4, count: 3});
  check('the window cuts the bars', view.rows.length === 3);
  check('every indicator is cut to match', view.ma['5'].length === 3 &&
    view.ma['20'].length === 3 && view.volMa['5'].length === 3 && view.macd.length === 3);
  check('indicators stay aligned with their bars', view.ma['5'][0] === 2);
  check('a gap inside the window is re-indexed', view.gaps.length === 1 &&
    view.gaps[0].index === 1);
  check('the re-indexed gap points at the resuming bar',
    view.rows[view.gaps[0].index].time === '090500');
  const outside = barsSliceView(payload, {start: 2, end: 4, count: 2});
  check('a gap on the window edge is dropped', outside.gaps.length === 0);
  const empty = barsSliceView({}, {start: 0, end: 0, count: 0});
  check('an empty payload slices without throwing',
    empty.rows.length === 0 && empty.gaps.length === 0 && empty.macd.length === 0);
}

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nall bars_chart checks passed');
