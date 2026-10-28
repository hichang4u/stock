'use strict';

// ── 상수 ────────────────────────────────────────────────────────────────
const TICKER_NAMES = {
  '005930':'삼성전자','000660':'SK하이닉스','035420':'NAVER',
  '005380':'현대차','000270':'기아','028260':'삼성물산',
  '035720':'카카오','003550':'LG',
};
const REASON_CLS = {TRAILING:'b-tr',HARD_STOP:'b-hs',TIMEOUT:'b-to',OPEN:'b-op'};
const REASON_LBL = {TRAILING:'트레일링 청산',HARD_STOP:'손절 청산',TIMEOUT:'시간 청산',OPEN:'진행 중'};

// ── 유틸 ─────────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const fmt = (n, dec=0) => n==null ? '—' : Number(n).toLocaleString('ko-KR',{minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtPct = n => n==null ? '—' : (n>=0?'+':'')+Number(n).toFixed(2)+'%';
const fmtM = n => n==null ? '—' : (n>=1e6 ? (n/1e6).toFixed(2)+'M' : fmt(n))+'원';
const fmtWon = n => n==null ? '—' : `${fmt(n)}<span class="u">원</span>`;
const fmtStep = n => n ? fmtPct(Number(n) * 100) : '<span style="color:var(--mu)">—</span>';
const cls = (el, c) => { el.className = el.className.replace(/\b(up|dn|flat)\b/g,''); el.classList.add(c); };
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const tickerName = (ticker, name) => name || (ticker ? TICKER_NAMES[ticker] : '') || '';
const stockText = (ticker, name, fallback='—') => {
  const code = ticker ? String(ticker) : '';
  const nm = tickerName(code, name);
  if(code && nm && nm !== code) return `${code} ${nm}`;
  return code || nm || fallback;
};

// localStorage 접근 가드 — 프라이빗 모드/쿼터 초과 시 예외를 삼킨다.
function lsGet(key) { try { return localStorage.getItem(key); } catch(e) { return null; } }
function lsSet(key, val) { try { localStorage.setItem(key, val); } catch(e) {} }

// ── 탭 전환 ──────────────────────────────────────────────────────────────
function go(id, btn) {
  document.querySelectorAll('.sc').forEach(s=>s.classList.remove('on'));
  document.querySelectorAll('.ttab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.menu-item').forEach(b=>b.classList.remove('on'));
  const screen = $('sc-'+id) || $('sc-today');
  screen.classList.add('on');
  if(btn) btn.classList.add('on');
  else {
    const menu = document.querySelector(`.menu-item[onclick*="'${id}'"]`);
    if(menu) menu.classList.add('on');
  }
  if (id==='selection') loadF1();
  if (id==='assets') {
    renderAssets(_lastStatus);
    loadAssets(false);
  }
  if (id==='orders') loadOrders();
  if (id==='history') loadHistory();
  if (id==='stats')   loadStats();
  if (id==='improve') loadImprove();
  if (id==='settings') loadSettings();
}

// ── 시계 ─────────────────────────────────────────────────────────────────
// ── Arc 게이지 ────────────────────────────────────────────────────────────
let _arcAnims = [];

// 세션 구간: 시작은 장전 08:40 고정, 종료는 F5 마감 청산 시각.
// 종료는 /api/settings(f5.timeout_time)에서 받아 갱신하고, 미수신 시 15:15로 폴백한다.
const SESSION_START_MIN = 8 * 60 + 40;
let _sessionEndMin = 15 * 60 + 15;  // F5 마감 청산 폴백(15:15)

// "HH:MM" / "HH:MM:SS" → 자정 기준 분. 파싱 실패 시 null.
function parseClockMin(str) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(String(str ?? '').trim());
  if (!m) return null;
  const h = Number(m[1]), min = Number(m[2]);
  if (h > 23 || min > 59) return null;
  return h * 60 + min;
}

// 아크 진행도 0..1 (구간 밖은 클램프, endMin<=startMin이면 0).
function sessionArcProgress(startMin, endMin, nowMin) {
  if (!(endMin > startMin)) return 0;
  return Math.max(0, Math.min(1, (nowMin - startMin) / (endMin - startMin)));
}

// 1/4·2/4·3/4 지점의 "HH:MM" 눈금 라벨 3개를 구간에서 계산.
function sessionArcTicks(startMin, endMin) {
  const p2 = n => String(n).padStart(2, '0');
  return [1, 2, 3].map(i => {
    const t = Math.round(startMin + (endMin - startMin) * i / 4);
    return p2(Math.floor(t / 60) % 24) + ':' + p2(t % 60);
  });
}

(function tick(){
  setTimeout(tick, 1000);
  try {
    const now = new Date();
    const kst = new Date(now.getTime()+9*3600*1000);
    const p = n=>String(n).padStart(2,'0');
    $('clk').textContent = p(kst.getUTCHours())+':'+p(kst.getUTCMinutes())+':'+p(kst.getUTCSeconds())+' KST';
    $('btm-now').textContent = p(kst.getUTCHours())+':'+p(kst.getUTCMinutes());
    updateArc(kst);
  } catch(e) {}
})();

function updateArc(kst) {
  const h=kst.getUTCHours(), m=kst.getUTCMinutes(), s=kst.getUTCSeconds();
  const p=n=>String(n).padStart(2,'0');
  $('arc-now').textContent = p(h)+':'+p(m)+' 현재';

  const nowMin=h*60+m+s/60;
  let prog = sessionArcProgress(SESSION_START_MIN, _sessionEndMin, nowMin);
  const elapsed = Math.max(0, Math.round(nowMin-SESSION_START_MIN));
  $('arc-elapsed').textContent = elapsed+'분 경과';

  drawArc(prog);
}

function themeVal(dark, light) {
  return document.documentElement.getAttribute('data-theme')==='light' ? light : dark;
}

function drawArc(prog) {
  const c=$('arc'); if(!c) return;
  const ctx=c.getContext('2d'), cx=140, cy=5, r=130;
  ctx.clearRect(0,0,280,145);

  ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,0,true);
  ctx.strokeStyle=themeVal('#2a2e39','#c8cbd6'); ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();

  if(prog>0){
    ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI*(1-prog),true);
    ctx.strokeStyle='rgba(247,166,0,.08)'; ctx.lineWidth=22; ctx.lineCap='round'; ctx.stroke();

    ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI*(1-prog),true);
    ctx.strokeStyle='#f7a600'; ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();
  }

  sessionArcTicks(SESSION_START_MIN, _sessionEndMin).forEach((lbl,i)=>{
    const p=(i+1)/4, a=Math.PI*(1-p), ri=r-22;
    const x=cx+ri*Math.cos(a), y=cy+ri*Math.sin(a);
    ctx.fillStyle=themeVal('#363a45','#4f5260'); ctx.font='10px Noto Sans KR,sans-serif';
    ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText(lbl,x,y);
  });

  // 이벤트 점들
  for(const dot of _arcAnims){
    const a=Math.PI*(1-dot.prog), x=cx+r*Math.cos(a), y=cy+r*Math.sin(a);
    ctx.beginPath(); ctx.arc(x,y,5,0,Math.PI*2);
    ctx.fillStyle=dot.color; ctx.fill();
    if(dot.label){
      ctx.fillStyle=dot.color; ctx.font='10px Noto Sans KR,sans-serif';
      ctx.textAlign='center'; ctx.textBaseline='top';
      ctx.fillText(dot.label,x,y+9);
    }
  }

  if(prog>0 && prog<1){
    const ca=Math.PI*(1-prog), cx2=cx+r*Math.cos(ca), cy2=cy+r*Math.sin(ca);
    ctx.beginPath(); ctx.arc(cx2,cy2,9,0,Math.PI*2);
    ctx.fillStyle='rgba(247,166,0,.15)'; ctx.fill();
    ctx.beginPath(); ctx.arc(cx2,cy2,4.5,0,Math.PI*2);
    ctx.fillStyle='#f7a600'; ctx.fill();
  }
}

// ── Status 업데이트 ───────────────────────────────────────────────────────
let _lastStatus = null;
let _lastAssets = null;
let _priceFlow = [];
let _priceFlowTicks = [];
let _priceFlowTicker = null;
let _trackingStopPending = false;
const PRICE_FLOW_TICK_WINDOW = 5000;  // 서버 tick 버퍼(live._TICK_HISTORY_MAX)와 동일
const PRICE_FLOW_VIEW_MIN = 20;       // 보유 중 슬라이딩 창 크기(분)
const PRICE_FLOW_SCALE_KEY = 'priceFlowScale';  // 세로 스케일 모드 저장 키
const PRICE_FLOW_RANGE_KEY = 'priceFlowRange';  // 가로 구간 모드 저장 키
// 'recent'=최근 PRICE_FLOW_VIEW_MIN분 슬라이딩, 'trade'=진입~마지막 전체 고정.
let _priceFlowRangeMode = lsGet(PRICE_FLOW_RANGE_KEY) === 'trade' ? 'trade' : 'recent';
// 'full'=전체 가시 범위, 'trailing'=트레일링 밴드 집중. localStorage에서 복원.
let _priceFlowScaleMode = lsGet(PRICE_FLOW_SCALE_KEY) === 'trailing' ? 'trailing' : 'full';

function applyStatus(d) {
  if(d.assets) _lastAssets = d.assets;
  if(_lastAssets && !d.assets) d.assets = _lastAssets;
  _lastStatus = d;

  // 탑바 심볼
  const observing = isObservingFlow(d);
  $('tb-sym').textContent = flowTicker(d) || '—';
  $('tb-sname').textContent = d.ticker ? tickerName(d.ticker, d.name)
    : observing ? `${tickerName(d.observe_ticker, d.observe_name)} · 관측 중` : '대기중';

  // 모드 뱃지
  const isReal = d.mode!=='PAPER';
  $('tb-mode').textContent = d.mode||'PAPER';
  $('tb-mode').style.color = isReal ? 'var(--dn)' : 'var(--br)';

  // KIS 상태 (WS 연결이 되거나 mode가 있으면 OK로 간주)
  const kisOk = d.ws_connected || d.position_status!=='IDLE';
  setLiStatus('kis', kisOk?'ok':'off');

  // WS 상태
  setLiStatus('ws', d.ws_connected?'ok':'off');

  // NTP 상태
  const ntpCls = {OK:'ok',WARN:'warn',CRIT:'err',ERROR:'err'}[d.ntp_level]||'off';
  setLiStatus('ntp', ntpCls);
  const ntpTxt = d.ntp_offset_ms>0 ? `NTP +${Math.round(d.ntp_offset_ms)}ms` : 'NTP';
  $('si-ntp').querySelector('span:last-child').textContent = ntpTxt;
  $('ntp-lbl').textContent = ntpTxt;

  // 상태 스트립
  const badge = $('st-badge');
  badge.textContent = {IDLE:'대기중',ENTERING:'진입중',HOLDING:'보유중',CLOSED:'청산됨'}[d.position_status]||d.position_status;
  badge.className = 'st-badge '+(d.position_status||'IDLE');
  $('st-tk').textContent  = flowTicker(d) || '';
  $('st-name').textContent = d.ticker ? tickerName(d.ticker, d.name)
    : observing ? `${tickerName(d.observe_ticker, d.observe_name)} · 미보유 관측 중` : '';
  const trackingStopBtn = $('tracking-stop-btn');
  if(trackingStopBtn) {
    const closed = d.position_status === 'CLOSED';
    trackingStopBtn.hidden = !closed;
    if(closed) {
      const manuallyStopped = Boolean(d.post_close_tracking_stopped);
      const trackingActive = Boolean(d.post_close_tracking_active);
      trackingStopBtn.disabled = _trackingStopPending || !trackingActive;
      trackingStopBtn.textContent = _trackingStopPending
        ? '종료 중…'
        : manuallyStopped ? '추적 종료됨' : trackingActive ? '추적 종료' : '추적 완료';
      trackingStopBtn.title = trackingStopTooltip(trackingActive);
    }
  }

  // PnL 필
  const pills = $('tv-pills');
  if(d.pnl_pct!=null){
    const up = d.pnl_pct>=0;
    pills.innerHTML = `<span class="tv-pnl ${up?'up':'dn'}">${fmtPct(d.pnl_pct)}</span>`;
  } else {
    pills.innerHTML = '';
  }

  // 포지션 그리드
  $('pv-entry').innerHTML = d.entry_price ? fmt(d.entry_price)+'<span class="u">원</span>' : '—';
  const curEl = $('pv-cur');
  curEl.innerHTML = d.current_price ? fmt(d.current_price)+'<span class="u">원</span>' : '—';
  curEl.className = 'pval'+(d.current_price&&d.entry_price ? (d.current_price>d.entry_price?' pup':' pdn') : '');
  const pnlEl = $('pv-pnl');
  pnlEl.textContent = fmtPct(d.pnl_pct);
  pnlEl.className = 'pval'+(d.pnl_pct==null?'':d.pnl_pct>=0?' pup':' pdn');
  $('pv-high').innerHTML = d.high_price ? fmt(d.high_price)+'<span class="u">원</span>' : '—';
  $('pv-qty').innerHTML  = d.remaining_qty!=null ? fmt(d.remaining_qty)+'<span class="u">주</span>' : '—';
  const amt = d.entry_price&&d.entry_qty ? d.entry_price*d.entry_qty : null;
  $('pv-amount').innerHTML = amt ? fmtM(amt) : '—';
  updatePriceFlow(d);

  // 플래그
  const trail = d.trailing_active;
  const hasStep = d.highest_step>0;
  updateFlag('fl-trail', trail, trail?'Step Trailing 활성':'Step Trailing', '#22d3ee');
  updateFlag('fl-step', hasStep, hasStep?`최고 스텝 +${(d.highest_step*100).toFixed(1)}%`:'최고 스텝', 'var(--br)');
  const stopPrice = d.trail_stop || d.hard_stop;
  const stopLbl = d.trailing_active&&d.trail_stop ? `Trail Stop ${fmt(d.trail_stop)}원` : (d.hard_stop?`Hard Stop ${fmt(d.hard_stop)}원`:'Hard Stop —');
  $('fl-stop-lbl').textContent = stopLbl;
  $('fl-stop').querySelector('.dot').className = 'dot'+(stopPrice?' off':'  off');

  // 바텀 파이프라인 상태 반영
  updatePipeline(d.position_status, d);
  renderAssetSummary(d);
  renderAssets(d);
}

function setLiStatus(id, cls) {
  const li = $('li-'+id);
  li.className = 'li '+cls;
  const dot = $('dot-'+id);
  dot.className = 'dot'+(cls==='ok'?'':cls==='warn'?' warn':cls==='err'?' err':' off');
}

function updateFlag(id, on, label, dotColor) {
  const el=$(id);
  el.className='flag'+(on?' on':'');
  const dot=el.querySelector('.dot');
  dot.style.background = on ? dotColor : 'var(--s2)';
  dot.className='dot'+(on?'':' off');
  el.querySelector('span:last-child') || (el.childNodes[1]&&(el.childNodes[1].textContent=label));
  el.lastChild.textContent=label;
}

function updatePipeline(status, pipeline) {
  const stages=['F1 스캔','F2 잠금','F3 진입','F4 Step Trailing','F5 마감 청산'];
  const activeIdx = Number.isInteger(pipeline?.pipeline_stage)
    ? pipeline.pipeline_stage
    : ({IDLE:0,ENTERING:2,HOLDING:3,CLOSED:4}[status]??0);
  const failed = pipeline?.pipeline_failed === true;
  const segs = stages.map((s,i)=>{
    const c = failed && i===activeIdx ? 'b-fail' : i<activeIdx ? 'b-done' : i===activeIdx ? 'b-active' : 'b-dim';
    return `<span class="${c}">${s}</span>`;
  });
  $('btm-pipeline').innerHTML = segs.join('<span class="b-arr">›</span>');
}

// ── F1 스캔 패널 ─────────────────────────────────────────────────────────
let _lastF1 = null;
let _selectionVerdictFilter = 'all';

const F1_STATUS_LABEL = {
  IDLE:'대기',
  RUNNING:'진행중',
  RETRYING:'재시도',
  DONE:'완료',
  NO_TARGET:'대상없음',
  FAILED:'오류',
};

function pctFromRatio(v) {
  return v==null ? null : Number(v) * 100;
}

function f1StepClass(status, idx) {
  if(status==='FAILED') return idx===0 ? 'fail' : '';
  if(status==='NO_TARGET') return idx < 3 ? 'done' : idx===3 ? 'fail' : '';
  if(status==='DONE') return 'done';
  if(status==='RUNNING' || status==='RETRYING') return idx < 2 ? 'done' : idx===2 ? 'active' : '';
  return idx===0 ? 'active' : '';
}

function candidateSizeValue(c) {
  return Number(c?.expected_amount || c?.avg_amount_5d || 0);
}

function renderF1SizeChart(candidates, selectedTicker) {
  const chart = $('f1-size-chart');
  if(!chart) return;
  const rows = (Array.isArray(candidates) ? candidates : [])
    .filter(c => c && c.ticker)
    .slice(0, 50);
  if(!rows.length) {
    chart.innerHTML = '<div class="f1-size-empty">후보 전체 목록은 선정 메뉴에서 확인</div>';
    return;
  }
  const maxSize = Math.max(...rows.map(candidateSizeValue), 1);
  chart.innerHTML = rows.map((c, idx) => {
    const size = candidateSizeValue(c);
    const h = Math.max(8, Math.round((size / maxSize) * 100));
    const isSelected = selectedTicker && String(c.ticker) === String(selectedTicker);
    const amount = size ? `${(size / 1e8).toFixed(1)}억` : '대금 없음';
    const label = `${idx + 1}. ${c.ticker} ${c.name || ''} · ${amount}`;
    return `<button class="f1-size-bar ${isSelected ? 'sel' : ''}" title="${esc(label)}" onclick="go('selection')" aria-label="${esc(label)}">
      <span class="f1-size-fill" style="height:${h}%"></span>
    </button>`;
  }).join('');
}

function renderF1(d) {
  _lastF1 = d;
  const status = d.status || 'IDLE';
  const state = $('f1-state');
  state.textContent = F1_STATUS_LABEL[status] || status;
  state.className = 'f1-state ' + status;

  const updated = d.updated_at ? d.updated_at.substring(11,19) : '—';
  const snapshot = d.snapshot_name ? `스냅샷 ${d.snapshot_name}` : '스냅샷 없음';
  const attempt = d.last_event?.attempt ? ` · ${d.last_event.attempt}회 시도` : '';
  $('f1-meta').textContent = `${snapshot} · ${updated}${attempt}`;

  const steps = [
    ['Ranking 조회', d.raw_count ?? 0],
    ['예상체결 보강', d.expected_valid ?? 0],
    ['Gap 3~7%', d.gap_pass ?? 0],
    ['유동성 정렬', d.liquidity_pass ?? 0],
    ['후보 확정', stockText(d.selected?.ticker, d.selected?.name)],
  ];
  $('f1-steps').innerHTML = steps.map((s,i)=>`
    <div class="f1-step ${f1StepClass(status, i)}">
      <div class="f1-step-top"><span class="f1-dot"></span>${esc(s[0])}</div>
      <div class="f1-step-val">${esc(s[1])}</div>
    </div>
  `).join('');

  const selected = d.selected;
  $('f1-summary').innerHTML = `
    <div><div class="f1-k">최종 후보</div><div class="f1-v ${selected?'up':''}">${selected ? esc(`${selected.ticker} ${selected.name||''}`) : '—'}</div></div>
    <div><div class="f1-k">최종 갭</div><div class="f1-v ${selected?'up':''}">${selected ? fmtPct(pctFromRatio(selected.gap_pct)) : '—'}</div></div>
    <div><div class="f1-k">예상체결 대금</div><div class="f1-v">${selected ? (Number(selected.expected_amount||0)/1e8).toFixed(1)+'억' : '—'}</div></div>
    <div><div class="f1-k">구간 보정</div><div class="f1-v br">${esc(`CORE ${d.core_gap||0} · HIGH ${d.high_gap_allowed||0}`)}</div></div>
  `;

  renderF1SizeChart(d.candidates, selected?.ticker);
  renderSelection(d);
}

function appendPriceFlowTick(ts, price, ticker) {
  const parsedTs = Date.parse(ts) || Date.now();
  const numericPrice = Number(price || 0);
  if(!numericPrice) return;
  if(ticker && ticker !== _priceFlowTicker) {
    _priceFlowTicker = ticker;
    _priceFlow = [];
    _priceFlowTicks = [];
  }
  _priceFlowTicks.push({ts: parsedTs, price: numericPrice});
  if(_priceFlowTicks.length > PRICE_FLOW_TICK_WINDOW) _priceFlowTicks.shift();

  const minuteTs = Math.floor(parsedTs / 60000) * 60000;
  const last = _priceFlow[_priceFlow.length - 1];
  if(last && last.ts === minuteTs) {
    last.price = numericPrice;
    last.tick_count = Number(last.tick_count || 0) + 1;
  } else {
    _priceFlow.push({ts: minuteTs, price: numericPrice, tick_count: 1});
  }
}

// 차트가 그릴 종목. 미보유 중에는 F4가 관측하는 종목(observe_ticker)이다 —
// 진입이 실패해도 관측은 15:20까지 이어진다(2026-10-23 빅텍).
function flowTicker(d) {
  return d?.ticker || d?.observe_ticker || null;
}
function isObservingFlow(d) {
  return !d?.ticker && Boolean(d?.observe_ticker);
}

function updatePriceFlow(d) {
  const ticker = flowTicker(d);
  if(ticker !== _priceFlowTicker) {
    _priceFlowTicker = ticker;
    _priceFlow = [];
    _priceFlowTicks = [];
  }
  // 신선도 가드: SSE 증분 추가 후에는 d가 지난 폴링 payload(stale)일 수 있다.
  // payload의 마지막 tick이 버퍼보다 오래됐으면 재파싱하지 않아 tick 유실과
  // tick당 O(n) 재가공을 모두 막는다. 새 /api/status 응답은 항상 통과한다.
  const payloadTicks = Array.isArray(d?.tick_history) ? d.tick_history : [];
  const payloadLastTs = payloadTicks.length
    ? (Date.parse(payloadTicks[payloadTicks.length - 1].ts) || 0) : 0;
  const bufferLastTs = _priceFlowTicks[_priceFlowTicks.length - 1]?.ts || 0;
  if(payloadTicks.length && payloadLastTs >= bufferLastTs) {
    _priceFlowTicks = payloadTicks
      .map(row => ({ts: Date.parse(row.ts) || Date.now(), price: Number(row.price || 0)}))
      .filter(row => row.price > 0)
      .slice(-PRICE_FLOW_TICK_WINDOW);
  }
  const payloadMinutes = Array.isArray(d?.minute_price_history) ? d.minute_price_history : [];
  const payloadLastMinuteTs = payloadMinutes.length
    ? (Date.parse(payloadMinutes[payloadMinutes.length - 1].ts) || 0) : 0;
  const bufferLastMinuteTs = _priceFlow[_priceFlow.length - 1]?.ts || 0;
  if(payloadMinutes.length && payloadLastMinuteTs >= bufferLastMinuteTs) {
    _priceFlow = payloadMinutes
      .map(row => ({
        ts: Date.parse(row.ts) || Date.now(),
        price: Number(row.price || 0),
        tick_count: Number(row.tick_count || 0),
      }))
      .filter(row => row.price > 0);
  }
  const price = Number(d?.current_price || 0);
  if(price > 0 && d?.position_status === 'HOLDING' && !_priceFlow.length) {
    appendPriceFlowTick(Date.now(), price, ticker);
  }
  requestPriceFlowDraw(d);
}

// tick 폭주 시에도 그리기는 최소 간격으로 1회만 수행 (마지막 상태로 코얼레싱)
const PRICE_FLOW_DRAW_MIN_INTERVAL_MS = 150;
let _flowPendingStatus = null;
let _flowDrawTimer = null;
let _flowLastDrawAt = 0;
function requestPriceFlowDraw(d) {
  _flowPendingStatus = d;
  if(_flowDrawTimer) return;
  const delay = Math.max(0, PRICE_FLOW_DRAW_MIN_INTERVAL_MS - (Date.now() - _flowLastDrawAt));
  _flowDrawTimer = setTimeout(() => {
    _flowDrawTimer = null;
    _flowLastDrawAt = Date.now();
    drawPriceFlow(_flowPendingStatus);
  }, delay);
}
function fmtFlowTime(ts) {
  const dt = new Date(ts);
  if(Number.isNaN(dt.getTime())) return '--:--';
  const p = n => String(n).padStart(2, '0');
  return `${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`;
}

function fmtFlowMinute(ts) {
  const dt = new Date(ts);
  if(Number.isNaN(dt.getTime())) return '--:--';
  const p = n => String(n).padStart(2, '0');
  return `${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

function downsampleFlowPoints(points, maxBuckets) {
  // 시간 버킷별 min/max만 남기는 다운샘플링 — 스파이크를 보존하면서
  // 그리기 점 수를 픽셀 폭 기준(≤ 2×버킷)으로 제한한다.
  if(points.length <= maxBuckets * 2) return points;
  const first = points[0].ts;
  const span = (points[points.length - 1].ts - first) || 1;
  const out = [];
  let bucketIdx = -1, lo = null, hi = null;
  const flush = () => {
    if(!lo) return;
    if(lo === hi) out.push(lo);
    else if(lo.ts <= hi.ts) out.push(lo, hi);
    else out.push(hi, lo);
  };
  for(const p of points) {
    const b = Math.min(maxBuckets - 1, Math.floor((p.ts - first) / span * maxBuckets));
    if(b !== bucketIdx) { flush(); bucketIdx = b; lo = p; hi = p; }
    else {
      if(p.price < lo.price) lo = p;
      if(p.price > hi.price) hi = p;
    }
  }
  flush();
  return out;
}

function parseTradeMarks(d) {
  // /api/status trade_marks(당일 체결 주문) → 차트 마커. filled_at 오름차순 유지.
  return (Array.isArray(d?.trade_marks) ? d.trade_marks : [])
    .map(m => ({
      ts: Date.parse(m.filled_at) || 0,
      price: Number(m.fill_price || 0),
      side: m.order_type,
      phase: m.order_phase,
      ticker: m.ticker,
    }))
    .filter(m => m.ts > 0 && m.price > 0
      && (!d?.ticker || String(m.ticker) === String(d.ticker)));
}

function parseViEvents(d) {
  // /api/status vi_events(당일 VI 발동/해제) → 차트 음영 밴드.
  // released_ts가 없으면 진행 중(end=0) — 그리기 쪽에서 창 끝까지 칠한다.
  return (Array.isArray(d?.vi_events) ? d.vi_events : [])
    .map(v => ({
      start: Date.parse(v.ts) || 0,
      end: v.released_ts ? (Date.parse(v.released_ts) || 0) : 0,
      prc: Number(v.vi_prc || 0),
    }))
    .filter(v => v.start > 0);
}

function priceFlowViewWindow(d, marks, rangeMode) {
  // 가로 구간 두 모드. 'recent'는 오른쪽 끝이 최신인 슬라이딩 창이고,
  // 'trade'는 진입~마지막 전체를 고정해 매수/매도 마커를 함께 리뷰한다.
  //
  // 청산 후에도 'recent'가 필요한 이유: 사후 관측이 15:15까지 이어지므로
  // 전체 고정이면 구간이 하루로 늘어난다. 2026-08-28은 09:01 청산 뒤
  // 11:25에 이미 2시간 24분이 한 캔버스에 눌려 들어갔다 — 그 상태로 장이
  // 끝나면 6시간이 된다. 그러면 정작 볼 진입·청산 구간이 왼쪽 끝 몇 픽셀로
  // 뭉개진다. 마커를 다시 보고 싶을 때 'trade'로 돌아간다.
  const mode = rangeMode === 'trade' ? 'trade' : 'recent';
  const firstDataTs = _priceFlowTicks[0]?.ts || _priceFlow[0]?.ts;
  const lastDataTs = _priceFlowTicks[_priceFlowTicks.length - 1]?.ts
    || _priceFlow[_priceFlow.length - 1]?.ts;
  const entryTs = Date.parse(d?.entry_at) || firstDataTs || Date.now();
  if(d?.position_status === 'CLOSED') {
    const firstMarkTs = marks?.length ? marks[0].ts : entryTs;
    const lastMarkTs = marks?.length ? marks[marks.length - 1].ts : 0;
    const edge = 30 * 1000; // 양끝 마커가 잘리지 않도록 30초 여백
    const tradeStart = Math.min(entryTs, firstMarkTs) - edge;
    const tradeEnd = Math.max(lastDataTs || 0, lastMarkTs, tradeStart + 60 * 1000) + edge;
    if(mode === 'trade') return {startTs: tradeStart, endTs: tradeEnd};
    // 오른쪽 끝은 지금이 아니라 마지막 데이터다 — 사후 관측이 멎으면 빈
    // 구간으로 흘러가지 않고 마지막 틱에 붙어 선다.
    return {startTs: Math.max(tradeStart, tradeEnd - PRICE_FLOW_VIEW_MIN * 60 * 1000),
            endTs: tradeEnd};
  }
  const endTs = Math.max(Date.now(), entryTs + 60 * 1000);
  if(mode === 'trade') return {startTs: entryTs, endTs};
  const startTs = Math.max(entryTs, endTs - PRICE_FLOW_VIEW_MIN * 60 * 1000);
  return {startTs, endTs};
}

function resizePriceFlowCanvas(c) {
  const ratio = window.devicePixelRatio || 1;
  const displayW = Math.max(320, Math.round(c.clientWidth || c.parentElement?.clientWidth || c.width));
  const displayH = Math.max(140, Math.round(c.clientHeight || c.height || 180));
  const pixelW = Math.round(displayW * ratio);
  const pixelH = Math.round(displayH * ratio);
  if(c.width !== pixelW || c.height !== pixelH) {
    c.width = pixelW;
    c.height = pixelH;
  }
  const ctx = c.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, W: displayW, H: displayH};
}

// 세로 스케일 도메인 헬퍼(순수). 전체(full)와 트레일링(trailing) 두 모드의
// 최종 패딩 도메인 {min, max, mode}를 반환한다. Node extract/eval 하네스에서
// 단독 실행되도록 Math 외 외부 의존이 없다.
//   mode:       'full' | 'trailing'
//   fullValues: number[] — 전체 모드에서 고려하는 모든 후보 가격
//   focus:      {trailStop, price, high} — 트레일링 밴드 기준값(무효 시 0/undefined)
function priceFlowYDomain(mode, fullValues, focus) {
  const clean = (Array.isArray(fullValues) ? fullValues : [])
    .map(Number).filter(v => v > 0);
  let lo, hi, applied = 'full';
  if (mode === 'trailing') {
    const ts = Number(focus && focus.trailStop) || 0;
    const pr = Number(focus && focus.price) || 0;
    const hp = Number(focus && focus.high) || 0;
    const band = [ts, pr, hp].filter(v => v > 0);
    // valid trail_stop이 있고 밴드에 2점 이상 있을 때만 트레일링 집중.
    if (ts > 0 && band.length >= 2) {
      lo = Math.min.apply(null, band);
      hi = Math.max.apply(null, band);
      applied = 'trailing';
    }
  }
  if (applied !== 'trailing') {  // fallback = 기존 전체 동작과 동일
    if (!clean.length) return {min: 0, max: 1, mode: 'full'};
    lo = Math.min.apply(null, clean);
    hi = Math.max.apply(null, clean);
  }
  if (lo === hi) { lo *= 0.998; hi *= 1.002; }  // 기존 degenerate 가드 유지
  const span = hi - lo;
  lo -= span * 0.12; hi += span * 0.12;         // 기존 12% 여백 유지
  return {min: lo, max: hi, mode: applied};
}

function syncPriceFlowScaleButtons() {
  const full = $('scale-full'), tr = $('scale-trailing');
  if(!full || !tr) return;
  const trailing = _priceFlowScaleMode === 'trailing';
  full.classList.toggle('on', !trailing);
  tr.classList.toggle('on', trailing);
  full.setAttribute('aria-pressed', String(!trailing));
  tr.setAttribute('aria-pressed', String(trailing));
}

function syncPriceFlowRangeButtons() {
  const recent = $('range-recent'), trade = $('range-trade');
  if(!recent || !trade) return;
  const isTrade = _priceFlowRangeMode === 'trade';
  recent.classList.toggle('on', !isTrade);
  trade.classList.toggle('on', isTrade);
  recent.setAttribute('aria-pressed', String(!isTrade));
  trade.setAttribute('aria-pressed', String(isTrade));
}

function setPriceFlowRange(mode) {
  _priceFlowRangeMode = mode === 'trade' ? 'trade' : 'recent';
  lsSet(PRICE_FLOW_RANGE_KEY, _priceFlowRangeMode);
  syncPriceFlowRangeButtons();
  drawPriceFlow(_lastStatus);   // 즉시 로컬 재렌더 — 네트워크/타이머 없음
}

function setPriceFlowScale(mode) {
  _priceFlowScaleMode = mode === 'trailing' ? 'trailing' : 'full';
  lsSet(PRICE_FLOW_SCALE_KEY, _priceFlowScaleMode);
  syncPriceFlowScaleButtons();
  drawPriceFlow(_lastStatus);   // 즉시 로컬 재렌더 — 네트워크/타이머 없음
}

function drawPriceFlow(d) {
  const c = $('price-flow');
  if(!c) return;
  const {ctx, W, H} = resizePriceFlowCanvas(c);
  ctx.clearRect(0, 0, W, H);
  const pad = {l:46,r:14,t:14,b:36};
  const chartW = W - pad.l - pad.r;
  const chartH = H - pad.t - pad.b;
  const marks = parseTradeMarks(d);
  const {startTs, endTs} = priceFlowViewWindow(d || {}, marks, _priceFlowRangeMode);
  const xAtTs = ts => pad.l + Math.max(0, Math.min(1, (Number(ts) - startTs) / (endTs - startTs || 1))) * chartW;
  const sub = $('flow-sub');
  const holding = d?.position_status === 'HOLDING';
  const closed = d?.position_status === 'CLOSED';
  const observing = isObservingFlow(d);
  const live = holding || observing;

  const spanMin = Math.max(1, (endTs - startTs) / 60000);
  // 창 크기에 맞춰 눈금 간격 선택: 20분 창이면 1분 눈금, 청산 후 장기 보유 리뷰면 확대
  const gridMin = spanMin <= 21 ? 1 : spanMin <= 45 ? 2 : spanMin <= 90 ? 5 : 10;

  const drawGrid = () => {
    ctx.strokeStyle = themeVal('rgba(120,123,134,.18)', 'rgba(79,82,96,.18)');
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    for(let i=0;i<4;i++){
      const y = pad.t + chartH * i / 3;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    }
    ctx.fillStyle = themeVal('#787b86', '#4f5260');
    ctx.font = '10px Noto Sans KR,sans-serif';
    ctx.textBaseline = 'top';
    const gridStepMs = gridMin * 60 * 1000;
    const pxPerMin = chartW / spanMin;
    // 시간 라벨은 겹치지 않도록 60px 이상 확보되는 "정각 분" 간격만 사용 (기본: 눈금 1분·라벨 5분)
    const labelMin = [1, 2, 5, 10, 15, 30].find(m => m >= gridMin && m * pxPerMin >= 60) || 60;
    const firstGridTs = Math.ceil(startTs / gridStepMs) * gridStepMs;
    for(let ts = firstGridTs; ts <= endTs; ts += gridStepMs) {
      const x = xAtTs(ts);
      const dt = new Date(ts);
      const minuteOfDay = dt.getHours() * 60 + dt.getMinutes();
      const isHour = minuteOfDay % 60 === 0;
      ctx.strokeStyle = isHour
        ? themeVal('rgba(120,123,134,.28)', 'rgba(79,82,96,.28)')
        : themeVal('rgba(120,123,134,.12)', 'rgba(79,82,96,.14)');
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + chartH); ctx.stroke();
      if(minuteOfDay % labelMin === 0 && x >= pad.l + 18 && x <= W - pad.r - 18) {
        ctx.textAlign = 'center';
        ctx.fillText(fmtFlowMinute(ts), x, pad.t + chartH + 8);
      }
    }
    const nowTs = Date.now();
    if(live && nowTs >= startTs && nowTs <= endTs) {
      const x = xAtTs(nowTs);
      ctx.strokeStyle = themeVal('rgba(247,166,0,.45)', 'rgba(247,166,0,.5)');
      ctx.setLineDash([3,4]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + chartH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#f7a600';
      ctx.textAlign = 'center';
      ctx.fillText('현재', x, pad.t + 2);
    }
    ctx.textBaseline = 'alphabetic';
  };

  drawGrid();

  // VI(변동성완화장치) 구간 — 발동~해제 음영 밴드 (가격선 아래 레이어)
  for(const vi of parseViEvents(d)) {
    const viEnd = vi.end || (holding ? Date.now() : endTs);
    if(viEnd < startTs || vi.start > endTs) continue;
    const x1 = xAtTs(Math.max(vi.start, startTs));
    const x2 = xAtTs(Math.min(viEnd, endTs));
    ctx.fillStyle = themeVal('rgba(239,83,80,.10)', 'rgba(239,83,80,.16)');
    ctx.fillRect(x1, pad.t, Math.max(2, x2 - x1), chartH);
    if(x2 - x1 >= 18) {
      ctx.fillStyle = themeVal('rgba(239,83,80,.85)', 'rgba(239,83,80,.9)');
      ctx.font = '10px Noto Sans KR,sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText('VI', (x1 + x2) / 2, pad.t + 2);
      ctx.textBaseline = 'alphabetic';
    }
  }

  // tick 버퍼(5,000건)가 20분을 못 담는 활발한 종목 대비:
  // 첫 tick 이전 구간은 분 단위 이력으로 채우고, 그 뒤는 원시 tick으로 그린다.
  const firstTickTs = _priceFlowTicks.length ? _priceFlowTicks[0].ts : Infinity;
  const minutePrefix = _priceFlow.filter(p => p.ts < firstTickTs);
  const points = minutePrefix.concat(_priceFlowTicks)
    .filter(p => p.ts >= startTs && p.ts <= endTs);
  const visibleMarks = marks.filter(m => m.ts >= startTs && m.ts <= endTs);
  const refs = [d?.entry_price, d?.high_price, d?.trail_stop, d?.hard_stop, d?.current_price]
    .map(Number).filter(v => v > 0);
  const values = points.map(p => p.price).concat(refs).concat(visibleMarks.map(m => m.price));

  if(!values.length || (!holding && !closed && !observing)) {
    const emptyText = closed ? '청산 완료 · 차트 데이터 없음' : '보유 포지션 없음';
    if(sub) sub.textContent = emptyText;
    ctx.fillStyle = themeVal('#787b86', '#4f5260');
    ctx.font = '12px Noto Sans KR,sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(emptyText, W / 2, H / 2);
    return;
  }

  const firstTs = points[0]?.ts;
  const lastTs = points[points.length - 1]?.ts;
  // 재시작 후 CLOSED에서는 tick이 없고 current_price도 null일 수 있다.
  // 점 → 현재가 → 마지막 매도 마커 → 마지막 매수 마커 순으로 대체해 0원 표기를 막는다.
  const lastSellPrice = [...visibleMarks].reverse().find(m => m.side === 'SELL')?.price;
  const lastBuyPrice = [...visibleMarks].reverse().find(m => m.side === 'BUY')?.price;
  const lastPrice = points[points.length - 1]?.price
    || Number(d.current_price || 0)
    || lastSellPrice || lastBuyPrice || 0;
  const timeLabel = firstTs && lastTs ? `${fmtFlowTime(firstTs)}-${fmtFlowTime(lastTs)}` : '시간 대기';
  if(sub) {
    const rangeLabel = _priceFlowRangeMode === 'trade'
      ? '거래 전체'
      : `최근 ${PRICE_FLOW_VIEW_MIN}분`;
    const stateLabel = closed
      ? `청산 완료${d.close_reason ? ` (${d.close_reason})` : ''} · ${rangeLabel}`
      : observing ? `미보유 관측 · ${rangeLabel}` : rangeLabel;
    const priceLabel = lastPrice > 0 ? ` · ${live ? '현재' : '마지막'} ${fmt(lastPrice)}원` : '';
    sub.textContent = `${fmtFlowMinute(startTs)}-${fmtFlowMinute(endTs)} · ${stateLabel} · 틱 ${points.length}개 · ${timeLabel}${priceLabel}`;
  }

  const dom = priceFlowYDomain(_priceFlowScaleMode, values, {
    trailStop: Number(d && d.trail_stop) || 0,
    price: lastPrice || Number(d && d.current_price) || 0,
    high: Number(d && d.high_price) || 0,
  });
  const min = dom.min, max = dom.max;
  const yAt = v => pad.t + (max - v) / (max - min) * chartH;

  const drawRef = (value, color, label) => {
    if(!value) return;
    const y = yAt(Number(value));
    ctx.strokeStyle = color; ctx.setLineDash([4,4]); ctx.beginPath();
    ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = '10px Noto Sans KR,sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(label, pad.l + 4, y - 4);
  };
  // 데이터 레이어는 플롯 사각형에 클리핑 — 트레일링 집중 모드에서 도메인 밖
  // 참조선/포인트/마커가 그리드·축·라벨 위로 새어나오지 않게 한다.
  ctx.save();
  ctx.beginPath(); ctx.rect(pad.l, pad.t, chartW, chartH); ctx.clip();
  drawRef(d.entry_price, '#f7a600', '진입');
  drawRef(d.trail_stop || d.hard_stop, '#ef5350', d.trail_stop ? 'Trail Stop' : 'Hard Stop');
  drawRef(d.high_price, '#7b9ef9', '최고');

  if(points.length > 1) {
    const drawPoints = downsampleFlowPoints(points, Math.max(120, Math.round(chartW)));
    ctx.strokeStyle = lastPrice >= Number(d.entry_price || lastPrice) ? '#26a69a' : '#ef5350';
    ctx.lineWidth = 2;
    ctx.beginPath();
    drawPoints.forEach((p,i) => {
      const x = xAtTs(p.ts), y = yAt(p.price);
      if(i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  const last = points[points.length - 1];
  if(last && holding) {
    ctx.fillStyle = Number(last.price) >= Number(d.entry_price || last.price) ? '#26a69a' : '#ef5350';
    ctx.beginPath(); ctx.arc(xAtTs(last.ts), yAt(last.price), 4, 0, Math.PI * 2); ctx.fill();
  }

  // 매수/매도 체결 마커 — 한국 증권 관례: 매수 ▲ 빨강, 매도 ▼ 파랑
  visibleMarks.forEach(m => {
    const x = xAtTs(m.ts), y = yAt(m.price);
    const isBuy = m.side === 'BUY';
    const color = isBuy ? '#ef5350' : '#5b8def';
    const dir = isBuy ? 1 : -1; // 매수는 점 아래에서 ▲, 매도는 점 위에서 ▼
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, y + dir * 4);
    ctx.lineTo(x - 5, y + dir * 12);
    ctx.lineTo(x + 5, y + dir * 12);
    ctx.closePath();
    ctx.fill();
    ctx.font = '10px Noto Sans KR,sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = isBuy ? 'top' : 'alphabetic';
    const label = isBuy ? (m.phase === 'PYRAMID_BUY' ? '추가매수' : '매수') : '매도';
    ctx.fillText(label, x, y + dir * 14);
  });
  ctx.textBaseline = 'alphabetic';
  ctx.restore();  // 데이터 레이어 클리핑 해제 — 축 라벨은 플롯 밖(pad.l 좌측)에 그린다

  ctx.fillStyle = themeVal('#787b86', '#4f5260');
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(fmt(max), pad.l - 6, pad.t + 4);
  ctx.fillText(fmt(min), pad.l - 6, pad.t + chartH);
}
window.addEventListener('resize', () => drawPriceFlow(_lastStatus));

function renderSelection(d) {
  if(!$('sel-tbody')) return;
  const status = d?.status || 'IDLE';
  $('sel-status').textContent = F1_STATUS_LABEL[status] || status;
  $('sel-status').className = 'sc2-val ' + (status === 'DONE' ? 'pup' : status === 'NO_TARGET' || status === 'FAILED' ? 'pdn' : '');
  $('sel-raw').textContent = d?.raw_count ?? 0;
  $('sel-expected').textContent = d?.expected_valid ?? 0;
  $('sel-selected').textContent = stockText(d?.selected?.ticker, d?.selected?.name);

  const rows = d?.candidates || [];
  const processRows = d?.selection_process || [];
  if(!rows.length) {
    renderSelectionFilters([]);
    $('sel-tbody').innerHTML = '<tr><td colspan="10" class="empty">F1 후보 스냅샷 없음</td></tr>';
    return;
  }
  renderSelectionFilters(rows);
  const stepByKey = Object.fromEntries(processRows.map(step => [step.key, step]));
  const picked = key => new Set((stepByKey[key]?.tickers || []).map(String));
  const selectedTickers = picked('f1');
  const lockedTickers = picked('f2');
  const finalTickers = picked('f3');
  const badgeHtml = ticker => {
    const key = String(ticker || '');
    const badges = [];
    if(selectedTickers.has(key)) badges.push('<span class="badge b-op">선정</span>');
    if(lockedTickers.has(key)) badges.push('<span class="badge b-tr">잠금</span>');
    if(finalTickers.has(key)) badges.push('<span class="badge b-hs">최종</span>');
    return badges.length ? `<div class="sel-badges">${badges.join('')}</div>` : '—';
  };
  const rowsWithVerdict = rows.map(c => ({...c, _verdict: selectionVerdict(c)}));
  const visibleRows = _selectionVerdictFilter === 'all'
    ? rowsWithVerdict
    : rowsWithVerdict.filter(c => c._verdict === _selectionVerdictFilter);
  if(!visibleRows.length) {
    $('sel-tbody').innerHTML = '<tr><td colspan="10" class="empty">해당 판정 후보 없음</td></tr>';
    return;
  }
  const candidateHtml = visibleRows.map(c=>{
    const isFinal = finalTickers.has(String(c.ticker || ''));
    const verdict = c._verdict;
    const pass = c.gap_allowed === true || verdict === '통과' || verdict === '고갭통과';
    const band = c.gap_band || ((c.gap_source || '').startsWith('expected') ? '예상체결' : '등락률');
    const amount = c.expected_amount || c.avg_amount_5d || null;
    const avgAmount = c.avg_amount_5d || null;
    return `<tr class="${isFinal ? 'sel-final-row' : ''}">
      <td class="sel-state-cell">${badgeHtml(c.ticker)}</td>
      <td>${esc(c.ticker)}</td>
      <td>${esc(c.name || TICKER_NAMES[c.ticker] || '')}</td>
      <td class="${pass?'pup':''}">${fmtPct(pctFromRatio(c.gap_pct))}</td>
      <td>${esc(band)}</td>
      <td>${fmtPct(pctFromRatio(c.ranking_gap_pct))}</td>
      <td>${c.expected_api_gap_pct==null ? '—' : fmtPct(pctFromRatio(c.expected_api_gap_pct))}</td>
      <td>${amount ? (Number(amount)/1e8).toFixed(1)+'억' : '—'}</td>
      <td>${avgAmount ? (Number(avgAmount)/1e8).toFixed(1)+'억' : '—'}</td>
      <td><span class="badge ${pass?'b-tr':'b-to'}">${esc(verdict || (pass?'통과':'제외'))}</span></td>
    </tr>`;
  }).join('');
  $('sel-tbody').innerHTML = candidateHtml;
}

function selectionVerdict(c) {
  const verdict = c?.verdict || '';
  const pass = c?.gap_allowed === true;
  return verdict || (pass ? '통과' : '제외');
}

function renderSelectionFilters(rows) {
  const el = $('sel-filters');
  if(!el) return;
  const verdicts = [...new Set(rows.map(selectionVerdict).filter(Boolean))];
  if(_selectionVerdictFilter !== 'all' && !verdicts.includes(_selectionVerdictFilter)) {
    _selectionVerdictFilter = 'all';
  }
  el.innerHTML = [
    `<button class="fp ${_selectionVerdictFilter==='all'?'on':''}" data-s-value="all">전체</button>`,
    ...verdicts.map(v => `<button class="fp ${_selectionVerdictFilter===v?'on':''}" data-s-value="${esc(v)}">${esc(v)}</button>`),
  ].join('');
  el.querySelectorAll('[data-s-value]').forEach(btn => {
    btn.onclick = () => {
      _selectionVerdictFilter = btn.dataset.sValue || 'all';
      renderSelection(_lastF1);
    };
  });
}

function positionAssetValues(d) {
  if(!d) return {stockValue:null, pnlAmount:null, total:null, cash:null, buyable:null, holdings:0, holdingsList:[]};
  const assets = d.assets || _lastAssets || {};
  const qty = Number(d.remaining_qty || 0);
  const cur = Number(d.current_price || 0);
  const entry = Number(d.entry_price || 0);
  const fallbackHolding = qty > 0 && d.ticker ? [{
    ticker: d.ticker,
    name: d.name || tickerName(d.ticker),
    qty,
    orderable_qty: qty,
    current_price: cur || null,
    evaluation_amount: cur > 0 ? qty * cur : null,
    pnl_amount: cur > 0 && entry > 0 ? (cur - entry) * qty : null,
    pnl_pct: d.pnl_pct,
  }] : [];
  const holdingsList = Array.isArray(assets.holdings) && assets.holdings.length ? assets.holdings : fallbackHolding;
  const stockValue = assets.stock_value != null ? Number(assets.stock_value) : (qty > 0 && cur > 0 ? qty * cur : null);
  const pnlAmount = assets.pnl_amount != null ? Number(assets.pnl_amount) : (qty > 0 && cur > 0 && entry > 0 ? (cur - entry) * qty : null);
  return {
    stockValue,
    pnlAmount,
    total: assets.total_asset != null ? Number(assets.total_asset) : stockValue,
    cash: assets.cash != null ? Number(assets.cash) : null,
    buyable: assets.buyable_cash != null ? Number(assets.buyable_cash) : null,
    buyableSource: assets.buyable_cash_source || null,
    holdings: assets.holdings_count != null ? Number(assets.holdings_count) : holdingsList.length,
    holdingsList,
  };
}

function buyableSourceLabel(source) {
  if(source === 'ord_psbl_cash') return '주문가능 현금';
  if(source === 'dnca_tot_amt') return '예수금 기준';
  if(source === 'prvs_rcdl_excc_amt') return '가수도 정산금 기준';
  return '출처 대기';
}

function assetSnapshotLabel(assets, freshLabel='자산 메뉴에서 상세') {
  if(!assets) return freshLabel;
  if(assets.snapshot_source === 'DB') {
    const t = shortTime(assets.captured_at);
    return t === '—' ? '마지막 저장 스냅샷' : `마지막 저장 ${t}`;
  }
  if(assets.snapshot_source === 'KIS') {
    const t = shortTime(assets.captured_at);
    return t === '—' ? freshLabel : `KIS 조회 ${t}`;
  }
  return freshLabel;
}

function renderAssetSummary(d) {
  const v = positionAssetValues(d);
  const assets = d?.assets || _lastAssets || null;
  $('as-total').innerHTML = v.total == null ? '—' : fmtWon(v.total);
  $('as-cash').innerHTML = v.cash == null ? '—' : fmtWon(v.cash);
  $('as-holdings').textContent = `${v.holdings}종목`;
  const pnl = $('as-pnl');
  pnl.innerHTML = v.pnlAmount == null ? '—' : fmtWon(v.pnlAmount);
  pnl.className = 'asset-v ' + (v.pnlAmount == null ? '' : v.pnlAmount >= 0 ? 'up' : 'dn');
  $('as-buyable').innerHTML = v.buyable == null ? '—' : fmtWon(v.buyable);
  if($('as-source')) $('as-source').textContent = assetSnapshotLabel(assets);
  $('order-buyable').textContent = '종목별 조회';
}

function renderAssets(d) {
  if(!$('asset-tbody')) return;
  const v = positionAssetValues(d);
  $('asset-total').textContent = v.total == null ? '—' : fmt(v.total);
  $('asset-cash').textContent = v.cash == null ? '—' : fmt(v.cash);
  $('asset-buyable').textContent = v.buyable == null ? '—' : fmt(v.buyable);
  const assets = d?.assets || _lastAssets || null;
  $('asset-buyable-source').textContent = `${buyableSourceLabel(v.buyableSource)} · 실매수 가능은 주문 전 조회 · ${assetSnapshotLabel(assets, 'KIS 현재 조회')}`;
  $('asset-stock').textContent = v.stockValue == null ? '—' : fmtM(v.stockValue);
  $('asset-pnl').textContent = v.pnlAmount == null ? '—' : fmt(v.pnlAmount);
  $('asset-pnl').className = 'sc2-val ' + (v.pnlAmount == null ? '' : v.pnlAmount >= 0 ? 'pup' : 'pdn');

  const rows = [];
  rows.push(`<tr><td>예수금</td><td>—</td><td>${v.cash == null ? '—' : fmt(v.cash)}</td><td>—</td><td><span class="badge ${v.cash == null ? 'b-to' : 'b-op'}">${v.cash == null ? 'API 대기' : '예수금 기준'}</span></td></tr>`);
  if(v.holdingsList.length) {
    v.holdingsList.forEach(h => {
      const isAuto = d?.ticker && String(h.ticker) === String(d.ticker);
      const pnl = h.pnl_amount == null ? null : Number(h.pnl_amount);
      rows.push(`<tr><td>${esc(stockText(h.ticker, h.name))}</td><td>${fmt(h.qty)}주</td><td>${h.evaluation_amount == null ? '—' : fmt(h.evaluation_amount)}</td><td class="${pnl == null ? '' : pnl >= 0 ? 'pup' : 'pdn'}">${pnl == null ? '—' : fmt(pnl)}</td><td><span class="badge ${isAuto ? 'b-op' : 'b-tr'}">${isAuto ? '자동매매' : '계좌보유'}</span></td></tr>`);
    });
    rows.push(`<tr><td>계좌 보유 합계</td><td>${fmt(v.holdings)}종목</td><td>${v.stockValue == null ? '—' : fmt(v.stockValue)}</td><td class="${v.pnlAmount == null ? '' : v.pnlAmount >= 0 ? 'pup' : 'pdn'}">${v.pnlAmount == null ? '—' : fmt(v.pnlAmount)}</td><td><span class="badge b-tr">KIS 잔고</span></td></tr>`);
  } else {
    rows.push('<tr><td>보유종목</td><td>0종목</td><td>—</td><td>—</td><td><span class="badge b-to">대기중</span></td></tr>');
  }
  $('asset-tbody').innerHTML = rows.join('');
  $('set-mode').textContent = d?.mode || 'PAPER';
}
function shortTime(s) {
  if(!s) return '—';
  const m = String(s).match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(s).slice(0, 8);
}

function orderStatusBadge(status) {
  const s = String(status || 'PENDING');
  if(s === 'FILLED') return '<span class="badge b-tr">체결</span>';
  if(s === 'PARTIAL_FILL') return '<span class="badge b-op">부분체결</span>';
  if(s === 'CANCELLED') return '<span class="badge b-to">취소</span>';
  if(s === 'FAILED') return '<span class="badge b-hs">실패</span>';
  if(s === 'UNCERTAIN') return '<span class="badge b-hs">MTS 즉시확인</span>';
  return '<span class="badge b-op">대기</span>';
}

function orderSideLabel(order) {
  const phase = String(order.order_phase || '');
  if(phase === 'CANCEL') return '취소';
  if(phase === 'PYRAMID_BUY') return '피라미딩';
  if(phase.includes('SELL')) return '매도';
  if(order.order_type === 'SELL') return '매도';
  return '매수';
}

function renderOrders(rows) {
  const orders = Array.isArray(rows) ? rows : [];
  if($('order-total')) $('order-total').textContent = fmt(orders.length);
  if($('order-filled')) $('order-filled').textContent = fmt(orders.filter(o=>o.status === 'FILLED').length);
  if($('order-pending')) $('order-pending').textContent = fmt(orders.filter(o=>o.status === 'PENDING' || o.status === 'PARTIAL_FILL').length);
  if($('order-closed')) $('order-closed').textContent = fmt(orders.filter(o=>o.status === 'CANCELLED' || o.status === 'FAILED').length);
  if($('order-uncertain')) $('order-uncertain').textContent = fmt(orders.filter(o=>o.status === 'UNCERTAIN').length);

  const body = $('orders-tbody');
  if(!body) return;
  if(!orders.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">오늘 주문 내역 없음</td></tr>';
    return;
  }
  body.innerHTML = orders.map(o => {
    const orderTickerName = tickerName(o.ticker, o.name);
    const auditSource = o.audit_source === 'entry_order_attempts' ? '진입 감사원장' : '';
    const reason = o.error_msg || o.error_code || (o.status === 'UNCERTAIN' ? 'MTS 즉시 확인' : '');
    const detail = [reason, auditSource].filter(Boolean).join(' · ') || '—';
    return `<tr>
      <td>${esc(o.kis_order_id || (o.id ? `DB#${o.id}` : '—'))}</td>
      <td>${esc(shortTime(o.ordered_at))}</td>
      <td>${esc(o.ticker || '—')} ${esc(orderTickerName)}</td>
      <td>${esc(orderSideLabel(o))}</td>
      <td>${fmt(o.order_qty)}</td>
      <td>${o.order_price == null ? '—' : fmt(o.order_price)}</td>
      <td>${o.fill_qty == null ? '—' : fmt(o.fill_qty)}</td>
      <td>${orderStatusBadge(o.status)}</td>
      <td>${esc(o.order_phase || '—')}</td>
      <td>${esc(detail)}</td>
    </tr>`;
  }).join('');
}

// ── 이벤트 로그 렌더 ─────────────────────────────────────────────────────
const LOG_EVENT_MAP = {
  TICK:{n:'틱 수신',cls:''},
  DAILY_STATE_RESET:{n:'새 거래일 상태 초기화(Daily State Reset)',cls:''},
  WS_CONNECTED:{n:'웹소켓 연결(WebSocket Connected)',cls:''},
  WS_DISCONNECTED:{n:'웹소켓 연결 끊김(WebSocket Disconnected)',cls:'lv-warn'},
  TOKEN_REFRESHED:{n:'KIS 토큰 갱신(Token Refreshed)',cls:''},
  TOKEN_LOADED_FROM_CACHE:{n:'KIS 토큰 캐시 로드(Token Loaded From Cache)',cls:''},
  TIME_SYNC_WARN:{n:'시각 오차 경고(Time Sync Warning)',cls:'lv-warn'},
  TIME_SYNC_OK:{n:'시각 동기화 정상(Time Sync OK)',cls:''},
  TIME_SYNC_ERROR:{n:'시각 동기화 실패(Time Sync Error)',cls:'lv-error'},
  TIME_SYNC_FALLBACK:{n:'시각 동기화 서버 재시도(Time Sync Fallback)',cls:'lv-warn'},
  F1_DONE:{n:'F1 필터 완료(F1 Done)',cls:''},
  F1_API_ERROR:{n:'F1 API 오류(F1 API Error)',cls:'lv-warn'},
  F1_FETCH_DONE:{n:'F1 API 조회 완료(F1 Fetch Done)',cls:''},
  F1_FILTER_EMPTY:{n:'F1 필터 결과 없음(F1 Filter Empty)',cls:''},
  F1_RETRY_WAIT:{n:'F1 재시도 대기(F1 Retry Wait)',cls:'lv-warn'},
  F1_EXPECTED_COMPARE:{n:'F1 예상체결 비교(F1 Expected Compare)',cls:''},
  F1_SNAPSHOT_SAVED:{n:'F1 후보 스냅샷 저장(F1 Snapshot Saved)',cls:''},
  F1_EXPECTED_QUOTE_ERROR:{n:'F1 예상가 조회 오류(F1 Expected Quote Error)',cls:'lv-warn'},
  NO_TARGET:{n:'대상 종목 없음(No Target)',cls:''},
  F2_SKIPPED:{n:'F2 종목 잠금 생략(F2 Skipped)',cls:'lv-warn'},
  TARGET_LOCKED:{n:'대상 종목 잠금(Target Locked)',cls:''},
  F3_SKIPPED:{n:'F3 진입 생략(F3 Skipped)',cls:'lv-warn'},
  F3_RECHECK:{n:'F3 진입 전 재검증(F3 Recheck)',cls:''},
  F3_ENTRY_BLOCKED:{n:'F3 진입 차단(F3 Entry Blocked)',cls:'lv-warn'},
  GAP_RECHECK_UNAVAILABLE:{n:'진입 전 갭 재검증 불가(Gap Recheck Unavailable)',cls:'lv-warn'},
  BUYABLE_QTY_QUERY_FAILED:{n:'매수가능수량 조회 실패(Buyable Quantity Query Failed)',cls:'lv-warn'},
  ENTRY_CANCEL_RELEASE_WAIT:{n:'취소 후 증거금 해제 대기(Entry Cancel Release Wait)',cls:''},
  ENTRY_PRE_ORDER_WAIT:{n:'진입 주문 전 대기(Entry Pre-order Wait)',cls:''},
  ENTRY_ORDER_SENT:{n:'진입 주문 전송(Entry Order Sent)',cls:''},
  ENTRY_RETRY_START:{n:'진입 재시도 시작(Entry Retry Start)',cls:'lv-warn'},
  ENTRY_RETRY_SKIPPED:{n:'진입 재시도 생략(Entry Retry Skipped)',cls:'lv-warn'},
  ENTRY_FILL_POLL_TIMEOUT:{n:'진입 체결조회 시간초과(Entry Fill Poll Timeout)',cls:'lv-warn'},
  ENTRY_CANCEL_SENT:{n:'진입 주문 취소 전송(Entry Cancel Sent)',cls:'lv-warn'},
  ENTRY_EXECUTED:{n:'진입 체결(Entry Executed)',cls:''},
  ENTRY_FAIL:{n:'진입 실패(Entry Failed)',cls:'lv-warn'},
  GAP_CHANGED:{n:'진입 전 갭 변동(Gap Changed)',cls:'lv-warn'},
  SLIPPAGE_GUARD:{n:'슬리피지 가드 발동(Slippage Guard)',cls:'lv-warn'},
  PYRAMID_EXECUTED:{n:'피라미딩 체결(Pyramid Executed)',cls:''},
  PYRAMID_SKIPPED:{n:'피라미딩 생략(Pyramid Skipped)',cls:''},
  PYRAMID_TIMEOUT:{n:'피라미딩 체결 시간 초과(Pyramid Timeout)',cls:'lv-warn'},
  TRAILING_STOP:{n:'트레일링 스탑 청산(Trailing Stop)',cls:''},
  HARD_STOP:{n:'하드 스탑 청산(Hard Stop)',cls:'lv-error'},
  TIMEOUT_CLOSE:{n:'타임아웃 청산(Timeout Close)',cls:''},
  TIMEOUT_RETRY:{n:'타임아웃 청산 재시도(Timeout Retry)',cls:'lv-warn'},
  TIMEOUT_ORDER_FAILED:{n:'타임아웃 청산 주문 실패(Timeout Order Failed)',cls:'lv-error'},
  PROCESS_RESTART_DETECTED:{n:'프로세스 재시작 감지(Process Restart Detected)',cls:'lv-warn'},
  ORDER_SMOKE_BUY_FILLED:{n:'주문 테스트 매수 체결(Order Smoke Buy Filled)',cls:''},
  ORDER_SMOKE_SELL_FILLED:{n:'주문 테스트 매도 체결(Order Smoke Sell Filled)',cls:''},
};

function renderLogs(logs) {
  const scroll = $('ev-scroll');
  if(!logs.length){ scroll.innerHTML='<div class="empty">이벤트 없음</div>'; return; }
  scroll.innerHTML = logs.map((l,i)=>{
    const info = LOG_EVENT_MAP[l.event]||{n:l.event,cls:''};
    const eventName = l.event_label || info.n;
    const t = l.ts ? l.ts.substring(11,19) : '';
    const detail = buildLogDetail(l);
    const level = String(l.level||'info').toLowerCase();
    const lvCls = (level==='crit'||level==='critical'||level==='error')?'lv-error':(level==='warn'||level==='warning')?'lv-warn':'lv-info';
    const cur = i===0 ? '<span class="ev-cur">▌</span>' : '';
    return `<div class="ev ${info.cls||lvCls}">
      <div class="ev-t">${t}</div>
      <div><div class="ev-n">${esc(eventName)}${cur}</div><div class="ev-d">${esc(detail)}</div></div>
    </div>`;
  }).join('');
}

function buildLogDetail(l) {
  const parts=[];
  if(l.ticker || l.name) parts.push(stockText(l.ticker, l.name));
  if(l.reason) parts.push(`사유 ${l.reason}`);
  if(l.order_id) parts.push(`주문 ${l.order_id}`);
  if(l.offset_ms!=null) parts.push(`+${l.offset_ms}ms ${l.level}`);
  if(l.ntp_server) parts.push(l.ntp_server);
  if(l.order_price) parts.push(`주문가 ${fmt(l.order_price)}원`);
  if(l.order_qty) parts.push(`주문 ${fmt(l.order_qty)}주`);
  if(l.sleep_sec!=null) parts.push(`대기 ${fmt(l.sleep_sec,1)}초`);
  if(l.entry_attempt!=null && l.max_attempts!=null) parts.push(`시도 ${fmt(l.entry_attempt)}/${fmt(l.max_attempts)}`);
  if(l.entry_price) parts.push(`진입 ${fmt(l.entry_price)}원`);
  if(l.exit_price)  parts.push(`청산 ${fmt(l.exit_price)}원`);
  if(l.pnl_pct!=null) parts.push(`P&L ${fmtPct(l.pnl_pct)}`);
  if(l.fill_qty)    parts.push(`${fmt(l.fill_qty)}주`);
  if(l.gap_pct)     parts.push(`갭 ${fmtPct(l.gap_pct)}`);
  if(l.cash!=null) parts.push(`현금 ${fmt(l.cash)}`);
  if(l.poll_attempts!=null) parts.push(`조회 ${fmt(l.poll_attempts)}회`);
  if(l.poll_last_output_count!=null) parts.push(`체결조회 ${fmt(l.poll_last_output_count)}건`);
  if(l.poll_last_matched===true) parts.push('주문매칭');
  if(l.poll_last_ccld_qty!=null && l.poll_last_ccld_qty>0) parts.push(`체결 ${fmt(l.poll_last_ccld_qty)}주`);
  if(l.rt_cd!=null) parts.push(`rt ${l.rt_cd}`);
  if(l.msg_cd) parts.push(l.msg_cd);
  if(l.poll_last_msg_cd) parts.push(`last ${l.poll_last_msg_cd}`);
  if(l.raw_count!=null) parts.push(`raw ${fmt(l.raw_count)}`);
  if(l.ranking_pass!=null) parts.push(`ranking ${fmt(l.ranking_pass)}`);
  if(l.expected_pass!=null) parts.push(`expected ${fmt(l.expected_pass)}`);
  if(l.final_pass!=null) parts.push(`final ${fmt(l.final_pass)}`);
  if(l.expected_valid!=null) parts.push(`보강 ${fmt(l.expected_valid)}`);
  if(l.mismatch_count!=null) parts.push(`불일치 ${fmt(l.mismatch_count)}`);
  if(l.count!=null && l.path) parts.push(`${fmt(l.count)}건 저장`);
  if(l.error)       parts.push(l.error.substring(0,40));
  if(l.poll_last_error) parts.push(l.poll_last_error.substring(0,40));
  if(l.msg1)        parts.push(String(l.msg1).substring(0,40));
  if(l.poll_last_msg1) parts.push(String(l.poll_last_msg1).substring(0,40));
  if(l.message)     parts.push(l.message.substring(0,60));
  if(l.token_prefix) parts.push(l.token_prefix);
  return parts.join(' · ')||l.event;
}

// ── 이력 렌더 ────────────────────────────────────────────────────────────
let _historyTrades = [];
let _historyStats = null;
let _historyFilters = {period:'all', reason:'all'};

function historyMonthKey() {
  const now = new Date();
  const kst = new Date(now.getTime()+9*3600*1000);
  return String(kst.getUTCFullYear()) + String(kst.getUTCMonth()+1).padStart(2,'0');
}

function filteredHistoryTrades() {
  const ym = historyMonthKey();
  return _historyTrades.filter(t => {
    if(_historyFilters.period === 'month' && String(t.date||'').substring(0,6) !== ym) return false;
    if(_historyFilters.reason !== 'all' && t.close_reason !== _historyFilters.reason) return false;
    return true;
  });
}

function setHistoryFilter(type, value, btn) {
  _historyFilters[type] = value;
  document.querySelectorAll(`[data-h-filter="${type}"]`).forEach(b=>b.classList.remove('on'));
  if(btn) btn.classList.add('on');
  renderHistory(filteredHistoryTrades(), _historyStats);
}

function renderHistory(trades, stats) {
  if(stats){
    $('h-total').textContent = stats.total||0;
    $('h-wr').textContent    = (stats.win_rate||0)+'%';
    $('h-wr').className      = 'sc2-val '+(stats.win_rate>=50?'pup':'pdn');
    $('h-avg').textContent   = fmtPct(stats.avg_pnl);
    $('h-avg').className     = 'sc2-val '+(stats.avg_pnl>=0?'pup':'pdn');
    $('h-maxloss').textContent = fmtPct(stats.max_loss);
  }

  const tbody=$('h-tbody');
  if(!trades.length){ tbody.innerHTML='<tr><td colspan="8" class="empty">거래 없음</td></tr>'; return; }
  tbody.innerHTML = trades.map(t=>{
    const reason = t.status==='OPEN'?'OPEN':(t.close_reason||'—');
    const rc = REASON_CLS[reason]||'b-to';
    const rl = REASON_LBL[reason]||reason;
    const pnlCls = t.pnl_pct==null?'':(t.pnl_pct>=0?'pup':'pdn');
    const name = tickerName(t.ticker, t.name);
    return `<tr>
      <td>${t.date||'—'}</td>
      <td>${t.ticker||'—'} <span style="color:var(--mu);font-size:11px">${esc(name)}</span></td>
      <td>${t.entry_price?fmt(t.entry_price):'—'}</td>
      <td>${t.exit_price?fmt(t.exit_price):'<span style="color:var(--mu)">—</span>'}</td>
      <td class="${pnlCls}">${fmtPct(t.pnl_pct)}</td>
      <td class="${t.highest_step?'pup':''}">${fmtStep(t.highest_step)}</td>
      <td class="${t.pyramided?'pup':''}">${t.pyramided?'✓':'<span style="color:var(--mu)">—</span>'}</td>
      <td><span class="badge ${rc}">${rl}</span></td>
    </tr>`;
  }).join('');
}

// ── Stats 렌더 ───────────────────────────────────────────────────────────
let _statsData = null;

function renderStats(s) {
  _statsData = s;
  $('d-pct').textContent = (s.win_rate||0)+'%';
  $('d-pct').className = 'd-pct ' + (s.total < 5 ? '' : s.win_rate>=50 ? 'pup' : 'pdn');
  $('d-lbl').textContent = `${s.wins}승 ${s.losses}패`;
  $('d-wins').textContent = `승 (${s.wins})`;
  $('d-losses').textContent = `패 (${s.losses})`;
  if($('stats-sample-note')) {
    $('stats-sample-note').textContent = sampleNote(s.total || 0);
  }

  // 월별 그리드
  const grid=$('monthly-grid');
  if(!s.monthly||!s.monthly.length){ grid.innerHTML='<div class="empty">데이터 없음</div>'; }
  else {
    grid.innerHTML = s.monthly.map(m=>{
      const yy=m.ym.substring(0,4), mm=m.ym.substring(4,6);
      const pc = m.sum_pnl>=0?'pup':'pdn';
      return `<div class="mcell"><div class="mname">${yy}.${mm}</div><div class="mpnl ${pc}">${fmtPct(m.sum_pnl)}</div><div class="mtr">${m.n}거래</div></div>`;
    }).join('');
  }

  drawDonut(s.wins, s.losses);
  drawBar(s.by_reason);
  renderFactorGrid(s);
}

function sampleNote(total) {
  if(!total) return '폐쇄 거래가 아직 없어 통계 판단을 대기합니다.';
  if(total < 5) return `표본 ${total}건: 이상 징후만 참고하고 전략 변경은 보류하세요.`;
  if(total < 20) return `표본 ${total}건: 경향 확인 단계입니다. 20건 이상부터 조정 판단을 권장합니다.`;
  return `표본 ${total}건: 전략 비교에 사용할 수 있는 구간입니다.`;
}

function drawDonut(wins, losses) {
  const c=$('donut'); if(!c) return;
  const ctx=c.getContext('2d'), cx=96, cy=96, r=72, inner=48;
  ctx.clearRect(0,0,192,192);
  const total=wins+losses||1, winA=wins/total*2*Math.PI;
  ctx.beginPath(); ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,r,-Math.PI/2,-Math.PI/2+winA);
  ctx.closePath(); ctx.fillStyle='#26a69a'; ctx.fill();
  ctx.beginPath(); ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,r,-Math.PI/2+winA,-Math.PI/2+2*Math.PI);
  ctx.closePath(); ctx.fillStyle='#2a2e39'; ctx.fill();
  ctx.beginPath(); ctx.arc(cx,cy,inner,0,2*Math.PI);
  ctx.fillStyle=themeVal('#1e222d','#eaecf2'); ctx.fill();
}

function drawBar(byReason) {
  const c=$('bar'); if(!c) return;
  const ctx=c.getContext('2d');
  ctx.clearRect(0,0,420,192);
  const reasonLabel = {TRAILING:'트레일링',TIMEOUT:'시간 청산',HARD_STOP:'손절 청산',SLIPPAGE_GUARD:'슬리피지',ENTRY_FAIL:'진입 실패',MANUAL:'수동 청산'};
  const reasonColor = {TRAILING:'#26a69a',TIMEOUT:'#787b86',HARD_STOP:'#ef5350',SLIPPAGE_GUARD:'#f7a600',ENTRY_FAIL:'#d65dff',MANUAL:'#7b9ef9'};
  const order = ['TRAILING','TIMEOUT','HARD_STOP','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL'];
  const keys = Object.keys(byReason || {}).sort((a,b)=>(order.indexOf(a)<0?99:order.indexOf(a))-(order.indexOf(b)<0?99:order.indexOf(b)));
  const data = keys.map((key,i)=>({lbl:reasonLabel[key]||key, val:byReason[key]?.avg_pnl||0, color:reasonColor[key]||['#26a69a','#787b86','#ef5350','#f7a600','#7b9ef9'][i%5], n:byReason[key]?.n||0}));
  if(!data.length) {
    ctx.fillStyle = themeVal('#787b86','#4f5260');
    ctx.font='12px Noto Sans KR,sans-serif';
    ctx.textAlign='center';
    ctx.fillText('데이터 없음',210,96);
    return;
  }
  const pad={t:24,r:20,b:48,l:48};
  const W=420,H=192,cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
  const allVals=data.map(d=>Math.abs(d.val)).filter(v=>v>0);
  const maxA=allVals.length ? Math.max(...allVals)*1.3 : 2.5;
  const zY=pad.t+cH*(maxA/(2*maxA));

  ctx.beginPath(); ctx.moveTo(pad.l,zY); ctx.lineTo(W-pad.r,zY);
  ctx.strokeStyle=themeVal('#363a45','#c8cbd6'); ctx.lineWidth=1; ctx.stroke();

  const slot=cW/data.length, bW=slot*0.38;
  data.forEach((d,i)=>{
    const x=pad.l+i*slot+(slot-bW)/2;
    const bH=Math.abs(d.val)/(2*maxA)*cH;
    const y=d.val>=0?zY-bH:zY;
    ctx.fillStyle=d.color+'18'; ctx.fillRect(x,y,bW,bH);
    ctx.fillStyle=d.color; ctx.fillRect(x,d.val>=0?zY-3:zY,bW,3);
    ctx.font='bold 13px Noto Sans KR,sans-serif'; ctx.textAlign='center';
    ctx.fillStyle=d.color;
    ctx.fillText((d.val>=0?'+':'')+d.val.toFixed(2)+'%',x+bW/2,d.val>=0?y-8:y+bH+13);
    ctx.font='10px Noto Sans KR,sans-serif'; ctx.fillStyle=themeVal('#787b86','#4f5260');
    ctx.fillText(d.lbl,x+bW/2,H-pad.b+13);
    ctx.fillText('('+d.n+'건)',x+bW/2,H-pad.b+25);
  });
  ctx.textAlign='right'; ctx.font='10px Noto Sans KR,sans-serif'; ctx.fillStyle=themeVal('#787b86','#4f5260');
  [-maxA,-maxA/2,0,maxA/2,maxA].forEach(v=>{
    const yp=pad.t+(maxA-v)/(2*maxA)*cH;
    ctx.fillText((v>0?'+':'')+v.toFixed(maxA>=10?0:1)+'%',pad.l-6,yp+3);
    ctx.beginPath(); ctx.moveTo(pad.l,yp); ctx.lineTo(pad.l+cW,yp);
    ctx.strokeStyle='#363a4540'; ctx.lineWidth=1; ctx.stroke();
  });
}

function renderFactorGrid(s) {
  const el = $('stats-factor-grid'); if(!el) return;
  const groups = [
    ['피라미딩', s.by_pyramided || {}],
    ['Step 도달', s.by_step || {}],
    ['진입 시간', Object.fromEntries((s.by_entry_hour || []).map(r => [r.hour + '시', {n:r.n, avg_pnl:r.avg_pnl}]))],
  ];
  el.innerHTML = groups.map(([name, rows]) => {
    const entries = Object.entries(rows);
    const body = entries.length
      ? entries.map(([k,v])=>`<div class="factor-row"><span>${esc(k)}</span><span class="${(v.avg_pnl||0)>=0?'pup':'pdn'}">${fmtPct(v.avg_pnl)} · ${fmt(v.n)}건</span></div>`).join('')
      : '<div class="empty">데이터 없음</div>';
    return `<div class="factor-cell"><div class="factor-name">${esc(name)}</div>${body}</div>`;
  }).join('');
}

function reasonName(reason) {
  return {TRAILING:'트레일링 청산',TIMEOUT:'시간 청산',HARD_STOP:'손절 청산',SLIPPAGE_GUARD:'슬리피지 차단',ENTRY_FAIL:'진입 실패',MANUAL:'수동 청산',MARKET_CLOSED:'휴장일',VI_ACTIVE:'변동성완화장치(VI) 진입 보류'}[reason] || reason;
}

function yn(v) {
  return v ? 'ON' : 'OFF';
}

function pctRange(v) {
  return Array.isArray(v) ? `${fmt(v[0], 1)}~${fmt(v[1], 1)}%` : '—';
}

function restBackupLabel(rb) {
  if (!rb || !rb.enabled) return '비활성';
  const poll = `${fmt(rb.poll_interval_sec, 1)}초 간격`;
  return rb.only_when_ws_stale ? `WS ${fmt(rb.ws_stale_sec, 1)}초 정지 시 · ${poll}` : `상시 · ${poll}`;
}

function settingBox(title, rows) {
  return `<div class="settings-box">
    <div class="settings-title">${esc(title)}</div>
    ${rows.map(([k,v,cls=''])=>`<div class="settings-row"><div class="settings-k">${esc(k)}</div><div class="settings-v ${cls}">${esc(v)}</div></div>`).join('')}
  </div>`;
}

function renderSettings(s) {
  if(!s) return;
  applySchedule(s);  // 설정 탭을 나중에 열 때도 세션 종료 시각을 재반영(중복 요청 없이 재사용)
  $('set-mode').textContent = s.mode || 'PAPER';
  $('set-mode').className = 'sc2-val ' + (s.mode === 'REAL' ? 'pdn' : '');
  $('set-runtime').textContent = s.dry_run ? 'DRY_RUN' : 'LIVE';
  $('set-runtime').className = 'sc2-val ' + (s.dry_run ? 'pup' : '');
  const readiness = s.real_readiness || {};
  const latchOpen = Boolean(readiness.runtime_entry_latch_open);
  $('set-readiness').textContent = `${fmt(readiness.percent || 0, 1)}%`;
  $('set-readiness').className = 'sc2-val ' + (readiness.eligible_for_real ? 'pup' : 'pdn');
  const realGateOpen = s.mode === 'REAL' && latchOpen;
  $('set-real-gate').textContent = s.mode === 'REAL'
    ? (realGateOpen ? '열림' : '차단')
    : (readiness.eligible_for_real ? '전환 가능' : '차단');
  $('set-real-gate').className = 'sc2-val ' + ((realGateOpen || (s.mode !== 'REAL' && readiness.eligible_for_real)) ? 'pup' : 'pdn');

  const grid = $('settings-grid');
  if(grid) {
    grid.innerHTML = [
      settingBox('REAL 준비도', [
        ['현재 점수', `${fmt(readiness.percent || 0, 1)} / 100`, readiness.eligible_for_real ? 'pup' : 'pdn'],
        ['코드 지문', readiness.strategy_fingerprint || '—'],
        ['런타임 매수 래치', latchOpen ? 'OPEN' : 'CLOSED', latchOpen ? 'pup' : 'pdn'],
        ['동일 버전 PAPER', `${fmt(readiness.clean_paper_trades || 0)} / ${fmt(readiness.required_paper_trades || 20)}회`],
        ...((readiness.groups || []).map(g => [g.label, `${fmt(g.earned || 0, 1)} / ${fmt(g.weight || 0, 1)}`])),
      ]),
      settingBox('F1 선정', [
        ['핵심 갭', pctRange(s.f1?.core_gap_pct)],
        ['고갭 조건', `${pctRange(s.f1?.high_gap_pct)} · 대금 ${fmt((s.f1?.high_gap_min_amount||0)/1e8, 0)}억 이상`],
        ['최소 후보', `${fmt(s.f1?.min_candidates)}개`],
        ['재시도', `${s.f1?.retry_deadline || '—'}까지 · ${fmt(s.f1?.retry_interval_sec)}초 간격`],
      ]),
      settingBox('F2/F3 진입', [
        ['F2 역할', '대상 락업'],
        ['투입 비중', `${fmt(s.f3?.alloc_ratio_pct, 1)}%`],
        ['1차/2차', `${fmt(s.f3?.first_ratio_pct, 0)}% / ${fmt(s.f3?.pyramid_ratio_pct, 0)}%`],
        ['주문 시각', `${s.f3?.first_order_at || '—'} · 2차 ${s.f3?.pyramid_at || '—'}`],
        ['재시도', `${fmt(s.f3?.max_attempts)}회 · ${s.f3?.retry_deadline || '—'}까지`],
        ['주문 전 갭 상한', `${fmt(s.f3?.order_gap_max_pct, 2)}%`],
        ['체결가 갭 상한', `${fmt(s.f3?.fill_gap_max_pct, 2)}%`],
      ]),
      settingBox('F4/F5 청산', [
        ['Hard Stop', `-${fmt(s.f4?.hard_stop_pct, 1)}%`],
        ['Step 간격', `+${fmt(s.f4?.step_size_pct, 1)}%`],
        ['Trail 폭', `-${fmt(s.f4?.step_trail_pct, 1)}%`],
        ['강제 트레일링', s.f4?.force_trailing_time || '—'],
        ['F5 마감 청산', s.f5?.timeout_time ? `${s.f5.timeout_time} · 점검 ${s.f5.precheck_time || '—'}` : '—'],
      ]),
      settingBox('시세 감시', [
        ['변동성완화장치(VI) 감시', s.vi?.watch_enabled ? '활성' : '비활성', s.vi?.watch_enabled ? 'pup' : 'pdn'],
        ['VI 발동 의심 기준', `가격 ${fmt(s.vi?.freeze_suspect_sec, 0)}초 멈춤`],
        ['VI 재조회 간격', `${fmt(s.vi?.check_cooldown_sec, 0)}초`],
        ['REST 백업', restBackupLabel(s.f4?.rest_backup), s.f4?.rest_backup?.enabled ? 'pup' : 'pdn'],
      ]),
      settingBox('경로/계정', [
        ['로그', s.paths?.logs || '—'],
        ['상태', s.paths?.state || '—'],
        ['F1 스냅샷', s.paths?.f1_snapshots || '—'],
        ['계좌번호', s.account?.configured ? `설정됨 (${s.account.account_source})` : '미설정', s.account?.configured ? 'pup' : 'pdn'],
        ['API 키', s.account?.app_key_configured && s.account?.app_secret_configured ? '설정됨' : '미설정', s.account?.app_key_configured && s.account?.app_secret_configured ? 'pup' : 'pdn'],
      ]),
    ].join('');
  }

  const alerts = [];
  (s.errors || []).forEach(v => alerts.push(['오류', v]));
  (s.warnings || []).forEach(v => alerts.push(['주의', v]));
  (readiness.blockers || []).forEach(v => alerts.push(['REAL 차단', v]));
  if(!alerts.length) alerts.push(['상태', '현재 설정에서 즉시 확인할 경고는 없습니다.']);
  const alertEl = $('settings-alerts');
  if(alertEl) {
    alertEl.innerHTML = alerts.map(([k,v])=>`<div class="hint-item"><div class="hint-k">${esc(k)}</div><div class="hint-v">${esc(v)}</div></div>`).join('');
  }
}

// ── API 호출 ─────────────────────────────────────────────────────────────
// 세션 종료(F5 마감 청산) 시각을 설정 페이로드에서 한 곳으로 반영한다.
// _sessionEndMin(아크 계산)·#arc-end·#btm-end 라벨을 함께 갱신하고 아크를 다시 그린다.
// timeout_time이 없거나 불량이면 기존 15:15 폴백을 유지한다.
function applySchedule(settings) {
  const raw = settings?.f5?.timeout_time;
  const end = parseClockMin(raw);
  if(end == null) return;
  _sessionEndMin = end;
  const hhmm = String(raw).slice(0, 5);
  const arcEnd = $('arc-end'); if(arcEnd) arcEnd.textContent = hhmm;
  const btmEnd = $('btm-end'); if(btmEnd) btmEnd.textContent = hhmm;
  drawArc(0);
}

async function loadSchedule() {
  try {
    const r = await fetch('/api/settings');
    if(!r.ok) return;
    applySchedule(await r.json());
  } catch(e){}
}

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    if(!r.ok) return;
    renderSettings(await r.json());
  } catch(e){}
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    if(!r.ok) return;
    applyStatus(await r.json());
  } catch(e){}
}

function trackingStopTooltip(trackingActive) {
  return trackingActive
    ? '매도 후 가격 관측을 즉시 종료합니다. 남은 시간의 가격 수집이 중단되며 오늘은 되돌릴 수 없습니다.'
    : '매도 후 가격 관측이 종료되었습니다.';
}

// 추적 종료 확인 문구. 관측을 그날 내내 되돌릴 수 없게 멈추므로, 남은 수집
// 시간과 비가역성을 안심 문구보다 먼저 말한다. 실제로 이 버튼 때문에
// 20260821·20260824의 가격 경로가 중간에 끊겨 검증 표본에서 빠졌다.
function trackingStopConfirmText(nowMin, sessionEndMin) {
  const p2 = n => String(n).padStart(2, '0');
  const endLabel = p2(Math.floor(sessionEndMin / 60)) + ':' + p2(sessionEndMin % 60);
  const left = Math.max(0, Math.round(sessionEndMin - nowMin));
  if(left <= 0) {
    return '매도 후 가격 추적을 종료할까요?\n\n'
      + `관측 창(${endLabel})이 이미 지나 남은 수집 시간이 없습니다.\n\n`
      + '이미 수집된 데이터와 차트는 유지됩니다.';
  }
  const h = Math.floor(left / 60), m = left % 60;
  const span = h ? (m ? `${h}시간 ${m}분` : `${h}시간`) : `${m}분`;
  return '매도 후 가격 추적을 종료할까요?\n\n'
    + `남은 관측 시간 ${span}(${endLabel}까지)이 수집되지 않습니다.\n`
    + '수집된 가격 경로는 전략 검증 데이터로 쓰이며,\n'
    + '한 번 종료하면 오늘은 다시 시작할 수 없습니다.\n\n'
    + '이미 수집된 데이터와 차트는 유지됩니다.';
}

async function stopPostCloseTracking() {
  if(_trackingStopPending || _lastStatus?.position_status !== 'CLOSED') return;
  const kst = new Date(Date.now() + 9 * 3600 * 1000);
  const nowMin = kst.getUTCHours() * 60 + kst.getUTCMinutes();
  if(!confirm(trackingStopConfirmText(nowMin, _sessionEndMin))) return;

  _trackingStopPending = true;
  applyStatus(_lastStatus);
  try {
    const r = await fetch('/api/tracking/stop', {
      method:'POST',
      headers:{'X-Tracking-Source':'dashboard'},
    });
    const result = await r.json().catch(() => ({}));
    if(!r.ok || !result.ok) {
      throw new Error(result.reason || `HTTP ${r.status}`);
    }
    await loadStatus();
    if(result.persisted === false) {
      alert('추적은 종료됐지만 상태 파일 저장에 실패했습니다. 재시작 시 다시 확인해 주세요.');
    }
  } catch(err) {
    alert(`추적 종료 실패: ${err?.message || err}`);
  } finally {
    _trackingStopPending = false;
    if(_lastStatus) applyStatus(_lastStatus);
  }
}

async function loadAssets(refresh) {
  const btn = $('asset-refresh');
  const meta = $('asset-updated');
  const setAssetMeta = (text, cls='idle', title='') => {
    if(!meta) return;
    meta.textContent = text;
    meta.className = 'status-chip ' + cls;
    meta.title = title || text;
  };
  const assetErrorLabel = err => {
    const msg = String(err?.message || '').trim();
    const code = msg.match(/\bmsg_cd=([^ ]+)/)?.[1] || String(err?.code || '').trim();
    const msg1 = msg.match(/\bmsg1=(.+)$/)?.[1];
    if(code && msg1) return `${code}: ${msg1}`;
    if(code) return code;
    const missing = msg.match(/missing field ([A-Za-z0-9_]+)/);
    if(missing) return `응답 필드 누락: ${missing[1]}`;
    if(msg.includes('missing output2')) return '잔고 요약 누락';
    if(msg.includes('output1 is not a list')) return '보유종목 응답 형식 오류';
    if(msg.includes('output1 item is not an object')) return '보유종목 항목 형식 오류';
    const invalid = msg.match(/invalid field ([A-Za-z0-9_]+)/);
    if(invalid) return `응답 숫자 오류: ${invalid[1]}`;
    return msg || '원인 미상';
  };
  try {
    if(refresh && btn) {
      btn.disabled = true;
      btn.textContent = '…';
      setAssetMeta('KIS 잔고 조회중', 'warn');
    }
    let r = await fetch('/api/assets' + (refresh ? '?refresh=1' : ''));
    let d = null;
    let missingAssetApi = false;
    if(r.status === 404) {
      missingAssetApi = true;
      r = await fetch('/api/status');
      if(!r.ok) throw new Error('asset api missing');
      d = await r.json();
      setAssetMeta('자산 API 미반영', 'warn');
    } else {
      if(!r.ok) throw new Error('asset api failed');
      d = await r.json();
    }
    if(d.assets) {
      _lastAssets = d.assets;
      _lastStatus = _lastStatus || {};
      _lastStatus.assets = d.assets;
      renderAssetSummary(_lastStatus);
      renderAssets(_lastStatus);
      if(!missingAssetApi) {
        const metaText = d.assets.snapshot_source === 'DB'
          ? assetSnapshotLabel(d.assets)
          : '최근 조회 ' + new Date().toLocaleTimeString('ko-KR', {hour12:false});
        setAssetMeta(metaText, d.assets.snapshot_source === 'DB' ? 'warn' : 'ok');
      }
    } else if(meta) {
      if(refresh && d.error) {
        const reason = assetErrorLabel(d.error);
        setAssetMeta('KIS 잔고 실패: ' + reason, 'warn', d.error.message || reason);
      } else {
        setAssetMeta(refresh ? 'KIS 잔고 응답 없음' : '자산 조회 대기', refresh ? 'warn' : 'idle');
      }
    }
  } catch(e) {
    setAssetMeta('자산 API 호출 실패', 'err');
  } finally {
    if(btn) {
      btn.disabled = false;
      btn.textContent = '↻';
    }
  }
}

function refreshAssets() {
  loadAssets(true);
}

async function loadLogs() {
  try {
    const r = await fetch('/api/logs?n=60');
    if(!r.ok) return;
    renderLogs(await r.json());
  } catch(e){}
}

async function loadF1() {
  try {
    const r = await fetch('/api/f1');
    if(!r.ok) return;
    renderF1(await r.json());
  } catch(e){}
}

async function loadOrders() {
  try {
    const r = await fetch('/api/orders');
    if(!r.ok) return;
    renderOrders(await r.json());
  } catch(e){}
}

async function loadHistory() {
  try {
    const [hr, sr] = await Promise.all([fetch('/api/history'), fetch('/api/stats')]);
    _historyTrades = hr.ok ? await hr.json() : [];
    _historyStats = sr.ok ? await sr.json() : null;
    const trades = filteredHistoryTrades();
    renderHistory(trades, _historyStats);
  } catch(e){}
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    if(!r.ok) return;
    renderStats(await r.json());
  } catch(e){}
}

// ── 개선(파라미터 진단) ──────────────────────────────────────────────────
// 판정 표본. 같은 지문 안에서만 파라미터가 하나라 판정은 'fingerprint'에서만 낸다
// (docs/IMPROVE_MENU_FOLLOWUP_20261028.md).
let _impScope = 'fingerprint';
function setImproveScope(scope, btn) {
  _impScope = scope;
  document.querySelectorAll('[data-imp-scope]').forEach(b=>b.classList.remove('on'));
  if(btn) btn.classList.add('on');
  loadImprove();
}

async function loadImprove() {
  try {
    const r = await fetch(`/api/improve?scope=${encodeURIComponent(_impScope)}`);
    if(!r.ok) return;
    renderImprove(await r.json());
  } catch(e){}
}

const IMP_BADGE = {
  ok:     {txt:'양호',      cls:'imp-ok'},
  watch:  {txt:'관찰',      cls:'imp-watch'},
  adjust: {txt:'조정 검토', cls:'imp-adjust'},
  nodata: {txt:'표본 부족', cls:'imp-nodata'},
  ref:    {txt:'참고',      cls:'imp-nodata'},
};
// H1 스펙 6절에서 닫힌 축 — 같은 표본으로 다시 열지 않는다.
const H1_CLOSED = 'H1 스펙 6절에서 닫힌 축이라 새 표본 없이 바꾸지 않습니다.';

function impCard(title, cur, [level, evidence, guide]) {
  const b = IMP_BADGE[level];
  return `<div class="imp-card">
    <div class="imp-head"><span class="imp-name">${esc(title)}</span>
      <span class="imp-cur">현재 ${esc(cur)}</span>
      <span class="imp-badge ${b.cls}">${b.txt}</span></div>
    <div class="imp-ev">${esc(evidence)}</div>
    <div class="imp-guide">${esc(guide)}</div>
  </div>`;
}

function judgeOverall(d) {
  // 판정은 성과 보고서와 같은 보수 비용 손익으로 한다(수수료·세금 + 청산 사유별 슬리피지).
  const o = d.overall_net || d.overall;
  const e = d.expectancy_by_basis || {};
  const ev = `기대값 체결 ${fmtPct(e.gross)} · 비용 ${fmtPct(e.cost)} · 보수 ${fmtPct(o.expectancy)} · 손익비(보수) ${(o.payoff_ratio||0).toFixed(2)} · 승률(보수) ${o.win_rate}% · 연속손실 ${o.cur_loss_streak}건(최대 ${o.max_loss_streak})`;
  if (o.total < 10) return ['nodata', ev, `최소 판단까지 ${10 - o.total}건 더 필요합니다.`];
  if (o.cur_loss_streak >= 3) return ['adjust', ev, `연속 손실 ${o.cur_loss_streak}건입니다(기준 3건). 일시 중단을 검토하세요.`];
  if (o.total < 20) return ['nodata', ev, `전략 조정 판단까지 ${20 - o.total}건 더 필요합니다. 현재 값은 경향만 참고하세요.`];
  if (o.expectancy < 0) return ['adjust', ev, '보수 비용 기대값이 음수입니다(20건 이상 누적). 파라미터 이전에 전략 자체를 재검토하세요.'];
  if (o.avg_loss < 0 && o.payoff_ratio < 1) return ['watch', ev, '손익비가 1 미만입니다(기준 1.0). 이긴 거래의 크기가 진 거래보다 작습니다.'];
  return ['ok', ev, '기대값·손익비·스트릭 모두 경고 기준 이내입니다.'];
}
function judgeHardStop(d) {
  const h = d.hard_stop;
  const ev = `손절 ${h.n}건(${h.share_pct}%) · 체결 편차 ${h.avg_slip_pp}%p · 10분 내 손절 ${h.fast_stop_n}건 · 평균 ${h.avg_min_to_stop}분`;
  if (h.n < 3) return ['nodata', ev, `판정까지 손절 표본 ${3 - h.n}건 더 필요합니다.`];
  if (h.avg_slip_pp > 0.3) return ['adjust', ev, `손절 체결이 설정(-${d.params.hard_stop_pct}%)보다 평균 ${h.avg_slip_pp}%p 밀립니다(기준 0.3%p). 주문 실행 경로를 점검하세요. 손절 폭은 ${H1_CLOSED}`];
  if (d.overall.total >= 10 && h.share_pct > 50) return ['watch', ev, `손절 비중이 ${h.share_pct}%로 절반을 넘습니다(기준 50%). 진입 품질은 H1 재평가에서 사전등록한 기준으로만 봅니다.`];
  if (h.fast_stop_n / h.n >= 0.5) return ['watch', ev, `손절의 ${Math.round(h.fast_stop_n / h.n * 100)}%가 진입 10분 내 발생 — 시초 변동성 구간입니다. 진입 시각은 ${H1_CLOSED}`];
  return ['ok', ev, '체결 편차·손절 비중 모두 기준 이내입니다.'];
}

function judgeStepSize(d) {
  const s = d.step;
  const ev = `관측 ${s.observed_n}/${d.overall.total}건(${s.coverage_pct}%) · 스텝1 ${s.step1_n}건(${s.step1_rate}%) · 근접 이탈 ${s.near_miss_n}건`;
  if (d.overall.total < 5) return ['nodata', ev, `최소 판단까지 ${5 - d.overall.total}건 더 필요합니다.`];
  if (s.observed_n < 5 || s.coverage_pct < 80) return ['nodata', ev, '고점 또는 스텝 데이터가 전체의 80% 미만입니다. 누락 데이터가 더 쌓인 뒤 판단하세요.'];
  if (s.near_miss_n >= 3 && s.near_miss_n > s.step1_n) return ['watch', ev, `고점 +1.5~${d.params.step_size_pct}%에서 손실로 끝난 거래(${s.near_miss_n}건)가 스텝1 도달(${s.step1_n}건)보다 많습니다. 스텝·트레일 간격은 ${H1_CLOSED}`];
  if (s.near_miss_n >= 2) return ['watch', ev, `근접 이탈이 ${s.near_miss_n}건 누적됐습니다(조정 기준 3건). 추이를 관찰하세요.`];
  return ['ok', ev, '관측된 근접 이탈이 없어 현재 간격을 유지합니다.'];
}
function judgeStepTrail(d) {
  const t = d.trailing;
  const x = d.trailing_diagnostics || {};
  const ev = `트레일링 ${t.n}건 · 활성 스탑 비교 ${x.stop_eval_n||0}건 · 평균 불리 편차 ${x.avg_stop_slip_pp||0}%p · 평균 고점 반납 ${t.avg_giveback_pp}%p(${x.giveback_n||0}건)`;
  if ((x.stop_eval_n||0) < 5) return ['nodata', ev, `판정까지 활성 스탑 비교 ${5 - (x.stop_eval_n||0)}건 더 필요합니다. 고점 반납의 구조적 범위는 ${x.structural_giveback_min_pp||0}~${x.structural_giveback_max_pp||0}%p입니다.`];
  if ((x.avg_stop_slip_pp||0) > 0.3 || (x.max_stop_slip_pp||0) > 0.5) return ['adjust', ev, '활성 스탑 목표보다 체결이 반복적으로 밀립니다. 트레일 폭보다 주문 실행 경로를 먼저 점검하세요.'];
  return ['ok', ev, `활성 스탑대로 청산되고 있습니다. 고점 반납 ${x.structural_giveback_min_pp}~${x.structural_giveback_max_pp}%p는 STEP_SIZE가 포함된 정상 구조 범위입니다.`];
}
function judgeSlipBuffer(d) {
  // 주문가 대비 체결가로 판정한다. 선정 기준가 대비 값은 진입 타이밍까지 섞여 참고로만 보인다.
  const ob = (d.order_slippage || {}).buy || {n:0, avg_pp:0, max_pp:0};
  const tb = d.slippage.buy;
  const bufPp = d.params.gap_max_fill_pct - d.params.gap_max_order_pct;
  const buf = bufPp.toFixed(1);
  const ev = `주문가 대비 매수 평균 ${ob.avg_pp}%p·최대 ${ob.max_pp}%p (${ob.n}건) · 기준가 대비 진입 평균 ${tb.avg_pp}%p·최대 ${tb.max_pp}%p · GUARD ${d.slippage.guard_n}건`;
  if (bufPp <= 0) return ['ref', ev, `주문 상한과 체결 상한이 같아(${d.params.gap_max_order_pct}%) 버퍼가 없습니다 — 버퍼 판정을 하지 않습니다. 기준가 대비 값은 진입 타이밍 지표입니다.`];
  if (ob.n < 3) return ['nodata', ev, `판정까지 매수 체결 ${3 - ob.n}건 더 필요합니다.`];
  if (d.slippage.guard_n >= 2 || ob.max_pp > 0.5) return ['adjust', ev, `주문가 대비 슬리피지가 버퍼(${buf}%p)를 위협합니다(GUARD ${d.slippage.guard_n}건, 최대 ${ob.max_pp}%p). GAP_MAX_ORDER 하향을 검토하세요.`];
  if (d.slippage.guard_n === 1) return ['watch', ev, `급격한 갭 확대 1건을 안전장치가 차단했습니다. 버퍼(${buf}%p)는 유지하고 재발 여부를 관찰하세요.`];
  if (ob.avg_pp > 0.25) return ['watch', ev, '주문가 대비 평균 매수 슬리피지가 0.25%p를 넘었습니다. 버퍼 소진 추이를 관찰하세요.'];
  return ['ok', ev, `주문가 대비 슬리피지가 버퍼(${buf}%p) 대비 여유 있습니다.`];
}
function judgeTimeout(d) {
  const to = d.timeout_exit;
  const net = to.avg_pnl_net == null ? to.avg_pnl : to.avg_pnl_net;
  const ev = `시간 청산 ${to.n}건 · 평균 손익 체결 ${fmtPct(to.avg_pnl)} / 보수 ${fmtPct(net)} · 평균 고점 ${fmtPct(to.avg_mfe)}`;
  if (to.n < 5) return ['nodata', ev, `판정까지 시간 청산 ${5 - to.n}건 더 필요합니다.`];
  if (net < 0) return ['adjust', ev, `시간 청산 보수 평균이 음수입니다 — 보유시간 내 회복에 실패하고 있습니다. 청산 시각(${d.params.timeout_time}) 단축을 검토하세요.`];
  if (to.avg_mfe >= 1.5) return ['watch', ev, `시간 청산 전 고점이 평균 +${to.avg_mfe}%였습니다(기준 1.5%). 강제 트레일링(${d.params.force_trailing_time}) 앞당김을 검토하세요.`];
  return ['ok', ev, '시간 청산 성과에 경고 신호가 없습니다.'];
}

function judgeGapRange(d) {
  const c = d.candidate_supply || {};
  const ev = `거래 ${c.trade_days||0}일 · 후보 없음 ${c.no_target_days||0}일 · 운영 실패 ${c.operational_skip_n||0}건 · 휴장 ${c.market_closed_days||0}일`;
  const range = `${d.params.f1_gap_min_pct}~${d.params.f1_gap_core_max_pct}% 코어 / ${d.params.f1_gap_core_max_pct}~${d.params.f1_gap_hard_max_pct}% 조건부`;
  const note = ' 진입 갭과 파라미터 버전이 거래별로 저장되면 성과 기반 정밀 판정이 가능합니다.';
  if ((c.evaluated_days||0) < 10) return ['nodata', ev, `판정까지 후보 공급 관측일 ${10 - (c.evaluated_days||0)}일 더 필요합니다.` + note];
  if ((c.no_target_days||0) > (c.trade_days||0)) return ['watch', ev, `실제 후보 없음이 거래일보다 많습니다. 현재 범위(${range}) 확대 여부를 신중히 검토하세요.` + note];
  return ['ok', ev, `실제 후보 공급은 정상입니다. 현재 범위(${range})를 유지하세요.` + note];
}
function scopeNote(sc) {
  if (!sc) return '';
  if (sc.name === 'fingerprint') {
    const since = sc.since ? ` · ${String(sc.since).slice(0, 16).replace('T', ' ')} 이후` : '';
    return `판정 표본: 현재 지문 ${sc.fingerprint || '—'}${since}`;
  }
  const label = sc.name === 'segment' ? `현재 구간 '${sc.segment}'` : '전체 기간';
  const unversioned = sc.unversioned_n ? ` + 지문 기록 전 ${sc.unversioned_n}건` : '';
  return `${label} · 지문 ${sc.fingerprints_n}개${unversioned} 섞임 — 참고 수치이며 판정하지 않습니다`;
}

// 같은 파라미터 표본(서버 _param_pools). 청산 카드는 청산 설정이, F1 카드는 F1 설정이 지금과 같았던
// 거래로 판정한다. 그 사이 진입 조건은 바뀌었으므로 카드에 밝힌다.
function poolNote(meta, label) {
  if (!meta) return '';
  const md = v => v ? `${String(v).slice(4, 6)}/${String(v).slice(6, 8)}` : '—';
  return `[${label} ${meta.n}건 · ${md(meta.first_date)}~${md(meta.last_date)} · 지문 ${meta.fingerprints_n}개, 진입 조건은 그 사이 바뀜] `;
}
function withPool(j, note) {
  return note ? [j[0], note + j[1], j[2]] : j;
}

function renderImprove(d) {
  const q = d.data_quality || {};
  const sc = d.scope || {verdict: true};
  const notes = [scopeNote(d.scope), sampleNote(d.overall.total || 0)].filter(Boolean);
  if (q.excluded_manual_n) notes.push(`수동 거래 ${q.excluded_manual_n}건 제외`);
  notes.push(`MFE 관측 ${q.mfe_observed_n||0}/${q.strategy_trade_n||0}건`);
  notes.push('손익 판정은 보수 비용 기준(성과 보고서와 같음)');
  if (sc.verdict && (d.exit_pool || d.f1_pool)) notes.push(`청산 카드는 같은 청산 설정 ${d.exit_pool ? d.exit_pool.meta.n : 0}건, F1 카드는 같은 F1 설정 ${d.f1_pool ? d.f1_pool.meta.n : 0}건으로 판정`);
  $('imp-sample-note').textContent = notes.join(' · ');
  const p = d.params;
  const refOnly = j => sc.verdict ? j : ['ref', j[1], '판정은 현재 지문 표본에서만 냅니다. 이 수치는 참고용입니다.'];
  const ex = sc.verdict ? d.exit_pool : null;
  const f1 = sc.verdict ? d.f1_pool : null;
  const dExit = ex ? {...d, hard_stop: ex.hard_stop, step: ex.step, trailing: ex.trailing,
    trailing_diagnostics: ex.trailing_diagnostics, timeout_exit: ex.timeout_exit,
    overall: {...d.overall, total: ex.overall_total}} : d;
  const dF1 = f1 ? {...d, candidate_supply: f1.candidate_supply} : d;
  const exNote = ex ? poolNote(ex.meta, '같은 청산 설정') : '';
  const f1Note = f1 ? poolNote(f1.meta, '같은 F1 설정') : '';
  const cards = [
    ['전략 종합',     `${d.overall.total}건`,                          judgeOverall(d)],
    ['HARD_STOP',    `-${p.hard_stop_pct}%`,                           withPool(judgeHardStop(dExit), exNote)],
    ['STEP_SIZE',    `+${p.step_size_pct}%`,                           withPool(judgeStepSize(dExit), exNote)],
    ['STEP_TRAIL',   `-${p.step_trail_pct}%`,                          withPool(judgeStepTrail(dExit), exNote)],
    ['슬리피지 · 진입가', `${p.gap_max_order_pct}→${p.gap_max_fill_pct}%`, judgeSlipBuffer(d)],
    ['F5 마감 청산', p.timeout_time,                                   withPool(judgeTimeout(dExit), exNote)],
    ['F1 갭 범위',   `${p.f1_gap_min_pct}~${p.f1_gap_core_max_pct}% / 조건부 ${p.f1_gap_hard_max_pct}%`, withPool(judgeGapRange(dF1), f1Note)],
  ];
  $('imp-cards').innerHTML = cards.map(([t, cur, j]) => impCard(t, cur, refOnly(j))).join('');
  renderMfeTable(d.mfe_rows);
  renderSlipTable(d.slippage, q);
  renderSkipHold(d);
}
function renderMfeTable(rows) {
  const tb = $('imp-mfe-tbody');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">폐쇄 거래 없음</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const mfe = r.mfe_pct == null ? '—' : fmtPct(r.mfe_pct);
    const gb = r.giveback_pp == null ? '—' : r.giveback_pp.toFixed(2) + '%p';
    const pc = (r.pnl_pct || 0) >= 0 ? 'pup' : 'pdn';
    return `<tr><td>${esc(r.date)}</td><td>${esc(r.name || r.ticker)}</td><td>${mfe}</td><td class="${pc}">${fmtPct(r.pnl_pct)}</td><td>${gb}</td><td>${esc(reasonName(r.close_reason))}</td></tr>`;
  }).join('');
}

function renderSlipTable(sl, quality = {}) {
  const tb = $('imp-slip-tbody');
  const phaseLabel = {FIRST_BUY:'1차 매수', PYRAMID_BUY:'피라미딩 매수', CLOSE_SELL:'청산 매도', PARTIAL_SELL:'부분 매도', TIMEOUT_SELL:'시간 청산 매도', SLIPPAGE_SELL:'슬리피지 청산'};
  const rows = Object.entries(sl.by_phase || {});
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty">체결 데이터 없음</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(([ph, v]) =>
    `<tr><td>${esc(phaseLabel[ph] || ph)}</td><td>${fmt(v.n)}건</td><td>${v.avg_pp}%p</td><td>${v.max_pp}%p</td><td>${quality.fill_latency_measured_n ? fmt(v.avg_latency_ms)+'ms' : '미측정'}</td></tr>`
  ).join('');
}

function renderSkipHold(d) {
  const el = $('imp-skip-hold');
  const skipLabel = {NO_TARGET:'후보 없음', GAP_CHANGED:'갭 이탈', ENTRY_FAIL:'진입 실패', SLIPPAGE_GUARD:'슬리피지', MANUAL:'수동', MARKET_CLOSED:'휴장일', VI_ACTIVE:'변동성완화장치(VI)'};
  const skipRows = Object.entries(d.candidates.skips || {})
    .map(([k, v]) => `<div class="factor-row"><span>${esc(skipLabel[k] || k)}</span><span>${fmt(v)}건</span></div>`)
    .join('') || '<div class="empty">스킵 없음</div>';
  const holdRows = Object.entries(d.hold_time || {})
    .map(([k, v]) => `<div class="factor-row"><span>${esc(reasonName(k))}</span><span>${v.avg_min}분 · ${fmt(v.n)}건</span></div>`)
    .join('') || '<div class="empty">데이터 없음</div>';
  el.innerHTML =
    `<div class="factor-cell"><div class="factor-name">스킵 사유</div>${skipRows}</div>` +
    `<div class="factor-cell"><div class="factor-name">청산사유별 평균 보유시간</div>${holdRows}</div>`;
}

// ── SSE 구독 ─────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if(d.type==='tick') {
        if(_lastStatus) {
          const tickTs = d.ts || new Date().toISOString();
          const tickTicker = d.ticker || flowTicker(_lastStatus);
          const tickPrice = Number(d.price || 0);
          _lastStatus.current_price = tickPrice;
          if(_lastStatus.entry_price)
            _lastStatus.pnl_pct = +((tickPrice/_lastStatus.entry_price-1)*100).toFixed(2);
          const observedTick = isObservingFlow(_lastStatus)
            && String(tickTicker) === String(_lastStatus.observe_ticker);
          if((_lastStatus.position_status === 'HOLDING' || observedTick) && tickPrice > 0) {
            // 배열 복사 없이 공유 버퍼(_priceFlowTicks/_priceFlow)에 tick 하나만 증분 추가.
            // updatePriceFlow의 신선도 가드가 이후 stale payload 덮어쓰기를 막는다.
            appendPriceFlowTick(tickTs, tickPrice, tickTicker);
          }
          applyStatus(_lastStatus);
        }
      } else if(d.type==='status') {
        loadStatus();
      } else if(d.type==='log') {
        loadLogs();
        loadF1();
        loadOrders();
      }
    } catch(err){}
  };
  es.onerror = () => { es.close(); setTimeout(connectSSE, 5000); };
}

// ── 테마 ─────────────────────────────────────────────────────────────────
function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  html.setAttribute('data-theme', next === 'dark' ? '' : 'light');
  $('theme-btn').textContent = next === 'light' ? '☀' : '🌙';
  localStorage.setItem('theme', next);
  // canvas는 CSS 변수를 읽지 못하므로 재렌더
  drawArc(0);
  drawPriceFlow(_lastStatus);
  if (_statsData) renderStats(_statsData);
}

(function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.addEventListener('DOMContentLoaded', () => {
      const btn = $('theme-btn');
      if (btn) btn.textContent = '🌙';
    });
  }
})();

// ── 초기 로드 ────────────────────────────────────────────────────────────
syncPriceFlowScaleButtons();  // 저장된 세로 스케일 모드로 버튼 상태 동기화
syncPriceFlowRangeButtons();  // 저장된 가로 구간 모드로 버튼 상태 동기화
loadSchedule();
loadStatus();
loadF1();
loadLogs();
loadOrders();
connectSSE();

// 폴링 백업 (SSE가 오래된 이벤트를 놓칠 경우 대비)
setInterval(loadStatus, 3000);
setInterval(loadF1, 5000);
setInterval(loadLogs, 10000);
setInterval(loadOrders, 5000);
