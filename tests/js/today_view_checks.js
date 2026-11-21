// 오늘 화면 표시 모델 검증 — app.js에서 순수 함수를 추출해 실제 실행한다.
// 실행: node tests\js\today_view_checks.js
// 2026-11-20 123690: 청산 후 관측가(6,420원 · 진입가 대비 −3.46%)가 거래 결과처럼 보였고
// DB 거래는 +5.22%(비용 전)였다. 토요일에는 그 지난 거래가 오늘 것처럼 섞였다.
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..', '..', 'docs', 'html');
const src = fs.readFileSync(path.join(root, 'assets', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'assets', 'app.css'), 'utf8');

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
function extractConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`${name} not found`);
  // 여러 줄 객체·함수 상수는 줄 첫머리의 '};'에서 끝난다.
  const lineEnd = src.indexOf('\n', start);
  const multi = src.slice(start, lineEnd).trimEnd().endsWith('{');
  const end = multi ? src.indexOf('\n};', start) + 3 : src.indexOf(';\n', start) + 1;
  return src.slice(start, end).replace(/^const /, 'var ');
}

for (const c of ['fmt', 'fmtPct', 'fmtM', 'tickerName', 'stockText', 'TICKER_NAMES',
                 'CLOSE_REASON_LABELS', 'F1_STATUS_LABEL', 'HEALTH_MARK']) eval(extractConst(c));
for (const f of ['mmdd', 'dateWithWeekday', 'hhmmss', 'signedWon', 'isObservingFlow',
                 'tradeCardModel', 'dayStripModel', 'ageText', 'healthModel', 'pctFromRatio',
                 'f1ViewModel', 'unresolvedOrderCount', 'logLevelCounts', 'shortTime',
                 'kstNowParts', 'assetSnapshotLabel', 'accountModeLabel']) eval(extract(f));

let failures = 0;
const check = (label, cond) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`);
  if (!cond) failures++;
};
const cellText = (m, label) => (m.cells.find(c => c.label === label) || {}).text;

// ── 1) 청산: 거래 결과와 청산 후 관측을 나눈다 ───────────────────────────
const closed = {
  position_status: 'CLOSED', ticker: '123690', entry_price: 6650, entry_qty: 9, remaining_qty: 0,
  current_price: 6420, pnl_pct: -3.46, high_price: 7010,
  observed_last_at: '2026-11-20T15:19:58.120000+09:00',
  trade_result: {available: true, closed: true, entry_price: 6650, exit_price: 6997, exit_qty: 9,
                 exit_at: '2026-11-20T09:01:53+09:00', close_reason: 'TRAILING',
                 pnl_amount: 3123, pnl_pct: 5.22, cost_basis: 'GROSS'},
};
let m = tradeCardModel(closed);
check('청산: 가격 칸은 청산가 6,997원', cellText(m, '청산가') === '6,997원');
check('청산: 손익 칸은 거래 손익(비용 전) +5.22%', /^\+5\.22% · \+3,123원$/.test(cellText(m, '거래 손익(비용 전)')));
check('청산: 대표 손익은 상승 색', m.cells[2].tone === 'up');
check('청산: 평가손익(-3.46%)은 대표 칸에 없다', !m.cells.some(c => String(c.text).includes('-3.46')));
check('청산: 관측 줄은 거래 결과 아님을 밝힌다', m.obs.includes('청산 후 관측(거래 결과 아님)'));
check('청산: 관측 줄에 마지막 관측가·시각', m.obs.includes('6,420원') && m.obs.includes('15:19:58'));
check('청산: 관측 줄에 진입가 대비 -3.46%', m.obs.includes('진입가 대비 -3.46%'));
check('청산: 메모에 비용 전·정산 확인 전', m.note.includes('수수료·세금 차감 전') && m.note.includes('트레일링'));
check('청산: 알약은 거래 +5.22% 비용 전', m.pill && m.pill.text === '거래 +5.22% 비용 전');
check('청산: 순실현손익이라고 부르지 않는다', !JSON.stringify(m).includes('순실현'));

// 결과 조회 실패·미확인 — 0원으로 그리지 않는다
m = tradeCardModel({...closed, trade_result: {available: false, reason: 'QUERY_FAILED'}});
check('조회 실패: 손익 칸은 조회 실패 문구', cellText(m, '거래 손익(비용 전)') === '거래 결과 조회 실패');
check('조회 실패: 청산가·손익 칸에 숫자 없음(0원으로 채우지 않음)', !/\d/.test(m.cells[1].text + m.cells[2].text));
check('조회 실패: 관측 줄은 그대로 분리', m.obs.includes('거래 결과 아님'));
m = tradeCardModel({...closed, trade_result: {available: false, reason: 'NO_TRADE'}});
check('행 없음: 확인 전 문구', cellText(m, '청산가') === '거래 결과 확인 전');

// ── 2) 보유·주문 중 상태의 의미 유지 ─────────────────────────────────────
m = tradeCardModel({position_status: 'HOLDING', ticker: '005930', entry_price: 10000, entry_qty: 5,
                    remaining_qty: 5, current_price: 10300, pnl_pct: 3, high_price: 10400});
check('보유: 평가손익(미실현) 라벨', m.cells[2].label === '평가손익(미실현)' && m.cells[2].text === '+3.00%');
check('보유: 현재가 칸', m.cells[1].label === '현재가' && m.cells[1].text === '10,300원');
check('보유: 관측 줄 없음', m.obs === '');
m = tradeCardModel({position_status: 'EXITING', ticker: '005930', entry_price: 10000, entry_qty: 5,
                    remaining_qty: 2, current_price: 9900, pnl_pct: -1});
check('청산 주문 중: 라벨과 안내', m.cells[2].label === '평가손익(청산 중)' && m.note.includes('청산 주문'));
check('청산 주문 중: 잔여수량 2주', m.cells[4].text === '2주');
m = tradeCardModel({position_status: 'ENTERING', ticker: '005930', entry_price: 10000});
check('진입 주문 중: 손익은 체결 확인 전', m.cells[2].text === '— (체결 확인 전)');
m = tradeCardModel({position_status: 'IDLE', observe_ticker: '005930', current_price: 10000});
check('미보유 관측: 관측가 라벨·미보유 안내', m.cells[1].label === '관측가' && m.note.includes('보유하지 않았습니다'));
m = tradeCardModel({position_status: 'IDLE'});
check('대기: 모든 값 —', m.cells.every(c => c.text === '—'));

// ── 3) 거래일·과거 자료 ────────────────────────────────────────────────
let day = dayStripModel({today: '20261121', today_weekday: '토', is_weekend: true,
                         data_date: '20261120', data_weekday: '금', is_previous_day: true}, '20261121');
check('토요일: 오늘 11/21(토)', day.todayText === '오늘 11/21(토)');
check('토요일: 직전 거래 기록 11/20(금)', day.data.includes('11/20(금)') && day.data.includes('직전 거래 기록'));
check('토요일: 장이 열리지 않음 안내', day.note.includes('주말'));
check('토요일: 세션 없음', day.sessionless === true);
day = dayStripModel({today: '20261120', today_weekday: '금', is_previous_day: false, late_open: true}, '20261120');
check('당일: 표시 자료 줄 없음', day.data === '');
check('10시 개장일 안내', day.note.includes('10시 개장일'));
day = dayStripModel(null, '20261121');
check('서버 session 없음: 브라우저 날짜로 오늘만', day.todayText === '오늘 11/21' && day.data === '');

// ── 4) 연결·시각: 미관측을 정상으로 그리지 않는다 ─────────────────────────
let h = healthModel({position_status: 'IDLE', health: {token: null, ntp: null, ws: null}}, '20261121');
check('기록 없음: KIS off', h.kis.level === 'off' && h.kis.text === '기록 없음');
check('기록 없음: 시각 off', h.ntp.level === 'off');
check('미보유·미연결: WS는 끊김 아님(off)', h.ws.level === 'off' && h.ws.text.includes('추적 대상 없음'));
h = healthModel({position_status: 'IDLE', health: {
  token: {ts: '2026-11-20T08:30:00+09:00'},
  ntp: {event: 'TIME_SYNC_OK', ts: '2026-11-20T08:30:01+09:00', offset_ms: 3}}}, '20261121');
check('지난 날 기록: 오늘 확인 전 + 마지막 날짜', h.kis.level === 'off' && h.kis.text.includes('11/20'));
check('지난 날 시각 점검: 오늘 점검 전', h.ntp.level === 'off' && h.ntp.text.includes('오늘 점검 전'));
h = healthModel({position_status: 'HOLDING', ws_connected: false, observed_last_at: '2026-11-20T10:00:01+09:00',
                 health: {ntp: {event: 'TIME_SYNC_WARN', ts: '2026-11-20T08:40:00+09:00', offset_ms: 230}}}, '20261120');
check('보유 중 WS 끊김: err + 마지막 시세 시각', h.ws.level === 'err' && h.ws.text.includes('10:00:01'));
check('오늘 시각 경고: warn + 오차', h.ntp.level === 'warn' && h.ntp.text.includes('230ms'));
h = healthModel({position_status: 'CLOSED', ws_connected: false, observed_last_at: '2026-11-20T15:19:58+09:00', health: {}}, '20261121');
check('지난 날 마지막 시세에는 날짜를 붙인다', h.ws.text.includes('11/20 15:19:58'));
check('상태 기호가 색과 함께 간다', HEALTH_MARK.ok === '✓' && HEALTH_MARK.err === '✕' && HEALTH_MARK.off === '–');

// ── 5) F1: 지난 거래를 오늘 후보 확정으로 그리지 않는다 ──────────────────
let f = f1ViewModel({status: 'IDLE', selection_source: 'PREVIOUS_TRADE', selection_trade_date: '20261120',
                     selected: {ticker: '123690', name: '한국화장품'}, today: '20261121'});
check('F1 지난 거래: 후보 확정 칸 —', f.steps[4][1] === '—' && f.selected === null);
check('F1 지난 거래: 요약에 직전 거래 표기', f.brief.includes('직전 거래 11/20') && f.brief.includes('오늘 선정 기록 없음'));
check('F1 스냅샷 없음: 단계 값 — (0 아님)', f.steps.slice(0, 4).every(s => s[1] === '—'));
check('F1 스냅샷 없음: meta', f.meta.startsWith('오늘 스냅샷 없음'));
check('F1 대기: 접힌 채 시작', f.open === 'auto-closed');
f = f1ViewModel({status: 'RUNNING', snapshot_name: '20261120_0900.json', updated_at: '2026-11-20T09:00:02+09:00',
                 raw_count: 30, expected_valid: 12, today: '20261120', snapshot_date: '20261120'});
check('F1 진행 중: 펼침', f.open === 'auto-open' && f.brief === '선정 진행 중');
check('F1 진행 중: 단계 값 숫자', f.steps[0][1] === 30);
f = f1ViewModel({status: 'FAILED', last_event: {attempt: 3}});
check('F1 실패: 펼침 + 오류 요약 + 시도 횟수', f.open === 'auto-open' && f.brief.includes('오류') && f.meta.includes('3회 시도'));
check('F1 실패·스냅샷 없음: 단계 수 —', f.steps.slice(0, 4).every(s => s[1] === '—'));
f = f1ViewModel({status: 'DONE', selection_source: 'TRADE_EXECUTED', selected: {ticker: '123690', name: '한국화장품'}});
check('F1 체결 종목·스냅샷 없음: 갭 생략, 단계 수 —', f.brief === '후보 123690 한국화장품' && f.steps[0][1] === '—');
f = f1ViewModel({status: 'DONE', snapshot_name: 's', selected: {ticker: '005930', gap_pct: 0.045}, raw_count: 10});
check('F1 완료: 후보·갭 요약', f.brief.includes('005930') && f.brief.includes('+4.50%'));
f = f1ViewModel({status: 'NO_TARGET', snapshot_name: 's', raw_count: 10});
check('F1 대상 없음 요약', f.brief === '조건을 통과한 후보 없음');

// ── 6) 미해결 주문·로그 요약 ────────────────────────────────────────────
check('미해결 주문: 접수·부분체결·불명만', unresolvedOrderCount([
  {status: 'PENDING'}, {status: 'PARTIAL_FILL'}, {status: 'UNCERTAIN'}, {status: 'FILLED'},
  {status: 'FAILED'}, {status: 'CANCELLED'}, null]) === 3);
check('미해결 주문: 잘못된 입력은 0', unresolvedOrderCount(undefined) === 0);
const lc = logLevelCounts([{level: 'ERROR'}, {level: 'crit'}, {level: 'WARN'}, {level: 'info'}, {}]);
check('로그 수준 집계', lc.error === 2 && lc.warn === 1 && lc.total === 5);

// ── 7) 자산 기준 시각·계좌 모드 문구 ─────────────────────────────────────
check('자산: 지난 날 스냅샷은 날짜를 붙인다',
  assetSnapshotLabel({snapshot_source: 'DB', captured_at: '2026-11-20T15:31:00+09:00'}, 'x', '20261121') === '마지막 저장 11/20 15:31:00');
check('자산: 오늘 스냅샷은 시각만',
  assetSnapshotLabel({snapshot_source: 'KIS', captured_at: '2026-11-21T09:00:00+09:00'}, 'x', '20261121') === 'KIS 조회 09:00:00');
check('계좌 모드 한글', accountModeLabel('PAPER') === '모의투자(PAPER)' && accountModeLabel('REAL') === '실계좌(REAL)');
check('경과 문구', ageText(4000) === '4초 전' && ageText(120000) === '2분 전');

// ── 8) 화면 구조: 거래일 → 대표 거래 → 가격 차트 → F1·이벤트 ────────────
const pos = id => html.indexOf(`id="${id}"`);
check('순서: 거래일 줄이 상태 줄보다 앞', pos('day-strip') > 0 && pos('day-strip') < pos('st-badge'));
check('순서: 대표 거래 칸이 가격 차트보다 앞', pos('pv-pnl') < pos('price-flow'));
check('순서: 가격 차트가 F1 상세보다 앞', pos('price-flow') < pos('f1-panel'));
check('F1 상세는 details(접기)', /<details class="f1-panel" id="f1-panel">/.test(html));
check('세션 막대는 progressbar', /id="sess-bar" role="progressbar"/.test(html));
check('게이지 버튼은 aria-expanded·aria-controls', /id="arc-toggle"[^>]*aria-expanded="false" aria-controls="arc-strip"/.test(html));
check('로그 접기 버튼은 aria-controls', /id="ev-toggle"[^>]*aria-controls="ev-scroll"/.test(html));
check('로그 요약은 role=status', /id="ev-sum" role="status"/.test(html));
check('청산 후 관측 줄 자리', pos('obs-strip') > pos('pv-amount'));
check('탑바 모드 한글', /id="tb-mode"[^>]*>모의투자</.test(html));
check('접힌 로그 띠에서 요약이 보인다', css.includes('.t-right.collapsed .ev-scroll{display:none;}')
  && !/\.t-right\.collapsed \.ev-sum\{[^}]*display:none/.test(css));
check('다크 --mu 대비 토큰', /--mu:#9598a1/.test(css));
check('라이트 상승/하락 글자색 토큰', /--up:#00695c;--dn:#b71c1c/.test(css));
check('범례 견본은 캔버스 고정색', css.includes('.lg-cur{background:var(--chart-up);}'));

console.log(failures ? `\n${failures} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(failures ? 1 : 0);
