(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var BT = String.fromCharCode(96);
  var FENCE = BT + BT + BT;
  var state = { taskId: null, es: null, running: false, kind: 'run', model: '' };

  // ------------------------------------------------ 格式化
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  // 浏览器 fetch 抛 TypeError 时只有一个原因：连不上本机服务（服务窗口被关了）。
  // 上游/base_url 报错是服务端返回的 JSON error，不会抛 TypeError，两者要分开说。
  function netError(e) {
    if (e && e.name === 'TypeError') {
      return '连不上本地服务：llm-stress.cmd 的窗口可能已经关了。重新双击 llm-stress.cmd 启动，再刷新本页面。原始错误：' + e.message;
    }
    return String((e && e.message) || e);
  }
  function fmtMs(v) {
    if (v === null || v === undefined || v === '') return '-';
    var n = Number(v);
    if (!isFinite(n)) return '-';
    return n >= 1000 ? (n / 1000).toFixed(2) + ' s' : Math.round(n) + ' ms';
  }
  function fmtNum(v, d) {
    if (v === null || v === undefined || v === '') return '-';
    var n = Number(v);
    if (!isFinite(n)) return '-';
    return n.toFixed(d === undefined ? 1 : d);
  }
  function fmtPct(v) {
    if (v === null || v === undefined) return '-';
    var n = Number(v);
    if (!isFinite(n)) return '-';
    return (n * 100).toFixed(1) + '%';
  }
  function list(title, items) {
    if (!items || !items.length) return '';
    return '<p class="hint" style="margin-top:10px">' + esc(title) + '</p><ul>' +
      items.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';
  }

  // ------------------------------------------------ 表单持久化
  var FIELDS = ['base_url', 'api_key', 'protocol', 'models', 'dataset', 'requests',
    'concurrency', 'start', 'step', 'stop', 'requests_per_stage', 'soak_concurrency',
    'duration_s', 'timeout_s', 'price_in_per_m', 'price_out_per_m'];
  var STORE_KEY = 'lst-form-v1';

  function saveForm() {
    var o = {};
    FIELDS.forEach(function (f) { var el = $('#' + f); if (el) o[f] = el.value; });
    try { localStorage.setItem(STORE_KEY, JSON.stringify(o)); } catch (e) {}
  }
  function loadForm() {
    var raw = null;
    try { raw = localStorage.getItem(STORE_KEY); } catch (e) {}
    if (!raw) return;
    try {
      var o = JSON.parse(raw);
      FIELDS.forEach(function (f) {
        var el = $('#' + f);
        if (el && o[f] !== undefined) el.value = o[f];
      });
    } catch (e) {}
  }

  // ------------------------------------------------ 场景切换
  function setScene(kind) {
    $('#scene').value = kind;
    Array.prototype.forEach.call(document.querySelectorAll('#scene-seg button'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-scene') === kind);
    });
    Array.prototype.forEach.call(document.querySelectorAll('label[data-for]'), function (l) {
      l.classList.toggle('hidden', l.getAttribute('data-for') !== kind);
    });
  }

  function collect() {
    var scene = $('#scene').value;
    var body = {
      base_url: $('#base_url').value.trim(),
      api_key: $('#api_key').value.trim(),
      protocol: $('#protocol').value,
      models: $('#models').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      scene: scene,
      dataset: $('#dataset').value,
      timeout_s: Number($('#timeout_s').value || 120),
      price_in_per_m: Number($('#price_in_per_m').value || 0),
      price_out_per_m: Number($('#price_out_per_m').value || 0),
      requests: Number($('#requests').value || 20),
      concurrency: Number($('#concurrency').value || 4),
      start: Number($('#start').value || 1),
      step: Number($('#step').value || 2),
      requests_per_stage: Number($('#requests_per_stage').value || 30),
      duration_s: Number($('#duration_s').value || 600)
    };
    if (scene === 'ladder') body.concurrency = Number($('#stop').value || 16);
    if (scene === 'soak') body.concurrency = Number($('#soak_concurrency').value || 4);
    return body;
  }

  // ------------------------------------------------ 运行状态
  function setBadge(kind, text) {
    var b = $('#status-badge');
    b.className = 'badge ' + kind;
    b.textContent = text;
  }
  function finish() {
    state.running = false;
    $('#btn-run').disabled = false;
    $('#btn-compare').disabled = false;
    $('#btn-cancel').disabled = true;
  }
  function resetRun() {
    $('#stage-table tbody').innerHTML = '';
    $('#live-stats').innerHTML = '';
    $('#verdict').className = 'verdict empty';
    $('#verdict').textContent = '压测进行中，结束后给出结论。';
    $('#compare-result').innerHTML = '';
    $('#compare-title').classList.add('hidden');
    $('#progress-bar').style.width = '0%';
  }

  function clearCompare() {
    $('#compare-result').innerHTML = '';
    $('#compare-title').classList.add('hidden');
  }

  function stat(k, v) {
    return '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + '</div></div>';
  }
  function onProgress(ev) {
    var pct = null;
    if (ev.planned) {
      pct = Math.min(100, (ev.sent / ev.planned) * 100);
    } else if (ev.duration_s) {
      pct = Math.min(100, (ev.elapsed_s / ev.duration_s) * 100);
    }
    if (pct !== null) $('#progress-bar').style.width = pct.toFixed(1) + '%';
    $('#status-text').textContent = (ev.stage_label || ('并发 ' + ev.concurrency)) +
      ' · 共 ' + ev.stage_count + ' 档 · 已发 ' + ev.sent +
      (ev.planned ? ('/' + ev.planned) : '') + ' · 用时 ' + ev.elapsed_s + 's';
    $('#live-stats').innerHTML = [
      stat('当前并发', ev.concurrency),
      stat('成功 / 失败', ev.ok + ' / ' + ev.fail),
      stat('RPS', fmtNum(ev.rps, 2)),
      stat('TTFT p50', fmtMs(ev.ttft_p50)),
      stat('总延迟 p50', fmtMs(ev.total_p50)),
      stat('当前模型', ev.current_model || '-')
    ].join('');
  }

  function onStageDone(msg) {
    var st = msg.stage;
    var sr = Number(st.success_rate);
    var cls = sr >= 0.99 ? 'ok' : (sr >= 0.95 ? 'warn' : 'bad');
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + st.concurrency + '</td>' +
      '<td>' + st.requests + '</td>' +
      '<td class="' + cls + '">' + fmtPct(sr) + '</td>' +
      '<td>' + fmtNum(st.rps, 2) + '</td>' +
      '<td>' + fmtNum(st.output_tps, 1) + '</td>' +
      '<td>' + fmtMs(st.ttft && st.ttft.p50) + '</td>' +
      '<td>' + fmtMs(st.ttft && st.ttft.p95) + '</td>' +
      '<td>' + fmtMs(st.total && st.total.p95) + '</td>' +
      '<td>' + fmtMs(st.tpot && st.tpot.p50) + '</td>';
    $('#stage-table tbody').appendChild(tr);
  }

  function verdictClass(v) {
    if (v === '可以承载') return 'ok';
    if (v === '容量受限') return 'warn';
    if (v === '不建议上线') return 'bad';
    return 'unknown';
  }

  function renderDecision(d, meta, drift, summary) {
    var box = $('#verdict');
    if (!d) {
      box.className = 'verdict empty';
      box.textContent = '没有产出结论数据。';
      return;
    }
    var cls = verdictClass(d.verdict);
    var html = '<div class="head">' +
      '<span class="title ' + cls + '">' + esc(d.verdict) + '</span>' +
      '<span class="badge">推荐并发 ' + (d.recommended_concurrency === null || d.recommended_concurrency === undefined ? '-' : d.recommended_concurrency) + '</span>' +
      '<span class="badge">峰值 ' + fmtNum(d.peak_output_tps, 1) + ' tok/s</span>';
    if (d.cost && d.cost.cost_per_mtok_usd) {
      html += '<span class="badge">$' + fmtNum(d.cost.cost_per_mtok_usd, 2) + ' /Mtok</span>';
    }
    html += '</div>';
    var slo = d.slo || {};
    html += '<p class="hint">SLO 口径：TTFT p95 ≤ ' + fmtMs(slo.ttft_p95_ms) +
      ' · 总延迟 p95 ≤ ' + fmtMs(slo.total_p95_ms) +
      ' · 成功率 ≥ ' + fmtPct(slo.success_rate) + '</p>';
    html += list('关键发现', d.findings);
    html += list('建议动作', d.advice);
    html += list('SLO 违规', d.violations);
    if (drift && drift.samples) {
      html += '<p class="hint">长稳漂移：' + (drift.drifting
        ? '后半段 p95 是前半段的 ' + fmtNum(drift.drift_ratio, 2) + ' 倍，存在劣化'
        : '前后半段基本一致（' + fmtNum(drift.drift_ratio, 2) + ' 倍）') +
        '，样本 ' + drift.samples + ' 条。</p>';
    }
    if (d.cost && d.cost.total_tokens) {
      html += '<p class="hint">累计消耗 ' + d.cost.total_tokens + ' tokens · 成本 $' +
        fmtNum(d.cost.total_cost_usd, 4) + '</p>';
    }
    if (d.error_mix && Object.keys(d.error_mix).length) {
      var parts = Object.keys(d.error_mix).map(function (k) { return k + '×' + d.error_mix[k]; });
      html += '<p class="hint">错误分布：' + esc(parts.join('，')) + '</p>';
    }
    if (meta && meta.started_at) {
      html += '<p class="hint">开始 ' + new Date(meta.started_at * 1000).toLocaleString() +
        ' · 协议 ' + esc(meta.protocol) + ' · 模型 ' + esc((meta.models || []).join(', ')) +
        ' · 数据集 ' + esc(meta.dataset) + '</p>';
    }
    if (summary && summary.files) {
      var files = ['markdown', 'json', 'samples'].map(function (k) {
        return summary.files[k] ? esc(summary.files[k]) : '';
      }).filter(Boolean);
      html += '<div class="files">' + files.join('<br>') + '</div>';
    }
    box.className = 'verdict';
    box.innerHTML = html;
  }

  function renderCompare(data) {
    var title = $('#compare-title');
    var box = $('#compare-result');
    if (!data || !data.entries) {
      title.classList.remove('hidden');
      box.innerHTML = '<div class="verdict empty">没有横评结果。</div>';
      return;
    }
    var ranked = data.ranking || [];
    var byModel = {};
    (data.entries || []).forEach(function (e) { byModel[e.model] = e; });
    var champ = ranked.length ? byModel[ranked[0]] : null;

    var html = '<div class="verdict"><div class="head">';
    if (champ) {
      html += '<span class="title ' + verdictClass(champ.verdict) + '">' + esc(champ.model) + '</span>' +
        '<span class="badge">' + esc(champ.verdict || '-') + '</span>' +
        '<span class="badge">推荐并发 ' + (champ.recommended_concurrency === null || champ.recommended_concurrency === undefined ? '-' : champ.recommended_concurrency) + '</span>' +
        '<span class="badge">峰值 ' + fmtNum(champ.peak_output_tps, 1) + ' tok/s</span>' +
        '<span class="badge">TTFT p95 ' + fmtMs(champ.ttft_p95) + '</span>';
    } else {
      html += '<span class="title bad">没有可用结果</span>';
    }
    html += '</div>';

    var meta = data.meta || {};
    html += '<p class="hint">共测 ' + (data.entries || []).length + ' 个模型 · 场景 ' + esc(data.scene || '-') +
      ' · 数据集 ' + esc(meta.dataset || '-') + '（' + (meta.dataset_size || 0) + ' 条）' +
      ' · 单请求超时 ' + fmtNum(meta.timeout_s, 0) + ' s · 逐模型串行执行' +
      (data.cancelled ? ' · <strong>本次被中途取消</strong>' : '') + '</p>';

    html += '<div class="tablewrap"><table><thead><tr>' +
      ['排名', '模型', '类型', '厂商', '结论', '推荐并发', '峰值 tok/s', 'TTFT p50', 'TTFT p95',
        '成功率', '成本/Mtok'].map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') +
      '</tr></thead><tbody>';
    ranked.forEach(function (name, i) {
      var e = byModel[name] || {};
      var cls = verdictClass(e.verdict);
      var sr = e.success_rate === null || e.success_rate === undefined ? null : Number(e.success_rate);
      html += '<tr>' +
        '<td>' + (i + 1) + '</td>' +
        '<td>' + esc(e.model || name) + '</td>' +
        '<td>' + esc(e.type_label || '-') + '</td>' +
        '<td>' + esc(e.family_label || '-') + '</td>' +
        '<td class="' + cls + '">' + esc(e.verdict || (e.error ? '未完成' : '-')) + '</td>' +
        '<td>' + (e.recommended_concurrency === null || e.recommended_concurrency === undefined ? '-' : e.recommended_concurrency) + '</td>' +
        '<td>' + fmtNum(e.peak_output_tps, 1) + '</td>' +
        '<td>' + fmtMs(e.ttft_p50) + '</td>' +
        '<td>' + fmtMs(e.ttft_p95) + '</td>' +
        '<td>' + (sr === null ? '-' : fmtPct(sr)) + '</td>' +
        '<td>' + (e.cost_per_mtok === null || e.cost_per_mtok === undefined ? '-' : '$' + fmtNum(e.cost_per_mtok, 2)) + '</td>' +
        '</tr>';
    });
    html += '</tbody></table></div>';

    html += '<p class="hint">排名依据：先比结论档位（可以承载 &gt; 容量受限 &gt; 不建议上线 &gt; 数据不足），' +
      '同档比推荐并发，再比峰值输出吞吐，最后比推荐档的 TTFT p95。</p>';

    var detail = '';
    ranked.forEach(function (name) {
      var e = byModel[name] || {};
      if (!e.findings && !e.advice && !e.error) return;
      detail += '<h4>' + esc(e.model || name) + '</h4>';
      if (e.error) detail += '<p class="hint">失败原因：' + esc(e.error) + '</p>';
      detail += list('关键发现', e.findings);
      detail += list('建议动作', e.advice);
    });
    if (detail) html += '<details><summary>各模型明细</summary>' + detail + '</details>';

    var files = data.files || {};
    if (files.markdown || files.json) {
      html += '<div class="files">' +
        (files.markdown ? esc(files.markdown) : '') +
        (files.json ? '<br>' + esc(files.json) : '') + '</div>';
    }
    html += '</div>';

    title.classList.remove('hidden');
    box.innerHTML = html;
  }

  function onModelStart(msg) {
    state.model = msg.model || '';
    var pct = msg.total ? (msg.index / msg.total) * 100 : 0;
    $('#progress-bar').style.width = pct.toFixed(1) + '%';
    $('#status-text').textContent = '横评 ' + (msg.index + 1) + '/' + msg.total +
      ' · ' + state.model + ' · 档位 ' + (msg.stages || []).join(' → ');
    $('#live-stats').innerHTML = [
      stat('当前模型', state.model),
      stat('进度', (msg.index + 1) + ' / ' + msg.total),
      stat('场景', msg.scene || '-'),
      stat('档位数', String((msg.stages || []).length))
    ].join('');
  }

  function onModelDone(msg) {
    var pct = msg.total ? ((msg.index + 1) / msg.total) * 100 : 100;
    $('#progress-bar').style.width = Math.min(100, pct).toFixed(1) + '%';
    $('#status-text').textContent = (msg.ok ? '完成 ' : '失败 ') + msg.model +
      (msg.ok ? (' · 结论 ' + (msg.verdict || '-') + ' · 推荐并发 ' +
        (msg.recommended_concurrency === null || msg.recommended_concurrency === undefined ? '-' : msg.recommended_concurrency)) :
        (' · ' + (msg.error || '')));
    $('#live-stats').innerHTML = [
      stat('当前模型', msg.model),
      stat('进度', (msg.index + 1) + ' / ' + msg.total),
      stat('结论', msg.ok ? (msg.verdict || '-') : '失败'),
      stat('峰值输出', msg.ok ? fmtNum(msg.peak_output_tps, 1) + ' tok/s' : '-'),
      stat('推荐并发', msg.recommended_concurrency === null || msg.recommended_concurrency === undefined
        ? '-' : String(msg.recommended_concurrency)),
      stat('状态', msg.ok ? '完成' : (msg.error || '失败'))
    ].join('');
  }

  function onDone(msg) {
    if (state.es) { state.es.close(); state.es = null; }
    setBadge(msg.status === 'cancelled' ? 'cancelled' : 'done',
      msg.status === 'cancelled' ? '已取消' : '完成');
    $('#status-text').textContent = '任务结束，报告已落盘';
    $('#progress-bar').style.width = '100%';
    if (msg.mode === 'compare') renderCompare(msg.compare);
    else renderDecision(msg.decision, msg.meta, msg.drift, msg.summary);
    finish();
    loadReports();
  }

  function onError(msg) {
    if (state.es) { state.es.close(); state.es = null; }
    setBadge('error', '出错');
    $('#status-text').textContent = msg.error || '未知错误';
    finish();
  }

  function subscribe(taskId) {
    if (state.es) { state.es.close(); state.es = null; }
    var es = new EventSource('/api/run/' + taskId + '/events');
    state.es = es;
    es.onmessage = function (ev) {
      var msg = null;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.kind === 'progress') onProgress(msg);
      else if (msg.kind === 'stage_done') onStageDone(msg);
      else if (msg.kind === 'model_start') onModelStart(msg);
      else if (msg.kind === 'model_done') onModelDone(msg);
      else if (msg.kind === 'done') onDone(msg);
      else if (msg.kind === 'error') onError(msg);
    };
  }

  async function startRun(ev) {
    if (ev) ev.preventDefault();
    if (state.running) return;
    var body = collect();
    if (!body.base_url) { alert('请填写 Base URL'); return; }
    if (!body.models.length) { alert('请填写至少一个模型'); return; }
    saveForm();
    resetRun();
    clearCompare();
    state.kind = 'run';
    state.model = '';
    $('#stage-wrap').classList.remove('hidden');
    $('#stage-title').classList.remove('hidden');
    setBadge('running', '运行中');
    $('#status-text').textContent = '正在创建任务…';
    $('#btn-run').disabled = true;
    $('#btn-compare').disabled = true;
    $('#btn-cancel').disabled = false;
    state.running = true;
    try {
      var res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(await res.text());
      var data = await res.json();
      state.taskId = data.task_id;
      $('#status-text').textContent = '任务 ' + data.task_id + ' · 共 ' +
        data.stages.length + ' 档（并发 ' + data.stages.join(' → ') + '）';
      subscribe(data.task_id);
    } catch (e) {
      setBadge('error', '启动失败');
      $('#status-text').textContent = netError(e);
      finish();
    }
  }

  async function startCompare(ev) {
    if (ev) ev.preventDefault();
    if (state.running) return;
    var body = collect();
    if (!body.base_url) { alert('请填写 Base URL'); return; }
    body.all_models = body.models.length === 0;
    body.limit_models = 8;
    saveForm();
    resetRun();
    clearCompare();
    state.kind = 'compare';
    state.model = '';
    $('#stage-wrap').classList.add('hidden');
    $('#stage-title').classList.add('hidden');
    setBadge('running', '横评中');
    $('#status-text').textContent = body.all_models
      ? '正在创建横评任务（自动挑选对话模型）…'
      : '正在创建横评任务…';
    $('#btn-run').disabled = true;
    $('#btn-compare').disabled = true;
    $('#btn-cancel').disabled = false;
    state.running = true;
    try {
      var res = await fetch('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(await res.text());
      var data = await res.json();
      state.taskId = data.task_id;
      $('#status-text').textContent = '横评任务 ' + data.task_id + ' · 共 ' +
        data.stages.length + ' 档（并发 ' + data.stages.join(' → ') + '）· 逐模型串行';
      subscribe(data.task_id);
    } catch (e) {
      setBadge('error', '启动失败');
      $('#status-text').textContent = netError(e);
      finish();
    }
  }

  async function cancelRun() {
    if (!state.taskId) return;
    try {
      await fetch('/api/run/' + state.taskId + '/cancel', { method: 'POST' });
      $('#status-text').textContent = '已请求取消，等当前请求收尾…';
      $('#btn-cancel').disabled = true;
    } catch (e) {}
  }

  // ------------------------------------------------ 模型列表
  function addModel(name) {
    if (!name) return;
    var el = $('#models');
    var cur = el.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (cur.indexOf(name) >= 0) return;
    if (cur.length >= 4) { alert('最多同时压 4 个模型'); return; }
    cur.push(name);
    el.value = cur.join(', ');
  }

  async function fetchModels() {
    var btn = $('#btn-models');
    btn.disabled = true;
    btn.textContent = '拉取中…';
    try {
      var res = await fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          base_url: $('#base_url').value.trim(),
          api_key: $('#api_key').value.trim(),
          protocol: $('#protocol').value
        })
      });
      var data = await res.json();
      var box = $('#model-list');
      var raw = data.models || [];
      // 新后端给的是带类型/厂商的明细对象；兼容旧后端返回的纯字符串数组。
      var items = raw.map(function (m) {
        if (typeof m === 'string') return { name: m, chat: true, type_label: '', family_label: '' };
        return m;
      });
      box.innerHTML = (data.error ? '<span class="hint">' + esc(data.error) + '</span>' : '') +
        items.slice(0, 200).map(function (m) {
          var hint = [m.type_label, m.family_label].filter(Boolean).join(' · ');
          return '<span class="chip' + (m.chat ? '' : ' off') + '" data-model="' + esc(m.name) +
            '" title="' + esc(hint || m.name) + '">' + esc(m.name) + '</span>';
        }).join('');
      var chat = data.chat_count;
      if (chat === undefined) chat = items.filter(function (m) { return m.chat; }).length;
      $('#model-count').textContent = items.length
        ? ('共 ' + items.length + ' 个模型，其中对话 ' + chat + ' 个 · 来源 ' + (data.path_used || '-'))
        : (data.error ? '' : (data.tried && data.tried.length ? '没取到模型列表，试过：' + data.tried.join('、') : '没取到模型列表'));
      Array.prototype.forEach.call(box.querySelectorAll('.chip'), function (c) {
        c.onclick = function () { addModel(c.getAttribute('data-model')); };
      });
    } catch (e) {
      $('#model-list').innerHTML = '<span class="hint">' + esc(netError(e)) + '</span>';
    } finally {
      btn.disabled = false;
      btn.textContent = '拉取模型';
    }
  }

  // ------------------------------------------------ 历史报告
  function renderMarkdown(md) {
    var lines = String(md || '').split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (line.indexOf(FENCE) === 0) {
        i++;
        var buf = [];
        while (i < lines.length && lines[i].indexOf(FENCE) !== 0) {
          buf.push(esc(lines[i]));
          i++;
        }
        i++;
        out.push('<pre><code>' + buf.join('\n') + '</code></pre>');
        continue;
      }
      if (/^\s*\|/.test(line)) {
        var rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
        out.push(tableHtml(rows));
        continue;
      }
      var h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        var lv = h[1].length;
        out.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>');
        i++;
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        var items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push('<li>' + inline(lines[i].replace(/^\s*[-*]\s+/, '')) + '</li>');
          i++;
        }
        out.push('<ul>' + items.join('') + '</ul>');
        continue;
      }
      out.push('<p>' + inline(line) + '</p>');
      i++;
    }
    return out.join('\n');
  }

  function inline(s) {
    var t = esc(s);
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    var codeRe = new RegExp(BT + '([^' + BT + ']+)' + BT, 'g');
    t = t.replace(codeRe, '<code>$1</code>');
    return t;
  }

  function tableHtml(rows) {
    if (rows.length < 2) return '<p>' + esc(rows.join(' ')) + '</p>';
    var cells = function (r) {
      var t = r.trim();
      if (t.charAt(0) === '|') t = t.slice(1);
      if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
      return t.split('|').map(function (c) { return c.trim(); });
    };
    var head = cells(rows[0]);
    var body = rows.slice(2);
    var html = '<table><thead><tr>' + head.map(function (c) {
      return '<th>' + inline(c) + '</th>';
    }).join('') + '</tr></thead><tbody>';
    body.forEach(function (r) {
      html += '<tr>' + cells(r).map(function (c) {
        return '<td>' + inline(c) + '</td>';
      }).join('') + '</tr>';
    });
    return html + '</tbody></table>';
  }

  async function openReport(li) {
    Array.prototype.forEach.call(document.querySelectorAll('#report-list li'), function (x) {
      x.classList.remove('on');
    });
    li.classList.add('on');
    var view = $('#report-view');
    view.innerHTML = '<p class="muted">读取中…</p>';
    try {
      var res = await fetch('/api/reports/' + encodeURIComponent(li.getAttribute('data-file')));
      var data = await res.json();
      view.innerHTML = renderMarkdown(data.markdown || '（该报告没有 Markdown 文件）');
      view.scrollTop = 0;
    } catch (e) {
      view.innerHTML = '<p class="muted">读取失败：' + esc(netError(e)) + '</p>';
    }
  }

  async function loadReports() {
    var ul = $('#report-list');
    try {
      var res = await fetch('/api/reports');
      var data = await res.json();
      var items = data.items || [];
      if (!items.length) { ul.innerHTML = '<li class="muted">还没有报告</li>'; return; }
      ul.innerHTML = items.map(function (it) {
        var t = it.time ? new Date(it.time * 1000).toLocaleString() : '';
        var isCompare = it.kind === 'compare';
        var tag = isCompare ? '[横评] ' : '';
        var extra = isCompare
          ? (' · ' + (it.models || 0) + ' 个模型，冠军 ' + (it.champion || '-'))
          : '';
        return '<li data-file="' + esc(it.file) + '">' +
          '<div class="rid">' + esc(tag + (it.run_id || it.file)) + '</div>' +
          '<div class="meta2">' + esc(it.scene || '-') + ' · ' + esc(it.verdict || '-') +
          ' · 推荐并发 ' + (it.recommended_concurrency === null || it.recommended_concurrency === undefined ? '-' : it.recommended_concurrency) +
          esc(extra) + '</div>' +
          '<div class="meta2">' + esc(t) + '</div></li>';
      }).join('');
      Array.prototype.forEach.call(ul.querySelectorAll('li'), function (li) {
        li.onclick = function () { openReport(li); };
      });
    } catch (e) {
      ul.innerHTML = '<li class="muted">加载失败：' + esc(netError(e)) + '</li>';
    }
  }

  // ------------------------------------------------ 数据集提示
  var DATASET_INFO = [];
  function updateDatasetHint() {
    var name = $('#dataset').value;
    var found = null;
    DATASET_INFO.forEach(function (it) { if (it.name === name) found = it; });
    $('#dataset-hint').textContent = found
      ? name + '：' + found.samples + ' 条样本，平均 ' + found.avg_input_chars +
        ' 字，合计约 ' + found.est_input_tokens + ' token 输入'
      : '';
  }

  // ------------------------------------------------ 启动
  async function boot() {
    loadForm();
    setScene($('#scene').value || 'ladder');
    Array.prototype.forEach.call(document.querySelectorAll('#scene-seg button'), function (b) {
      b.onclick = function () { setScene(b.getAttribute('data-scene')); saveForm(); };
    });
    $('#run-form').onsubmit = startRun;
    $('#btn-compare').onclick = startCompare;
    $('#btn-cancel').onclick = cancelRun;
    $('#btn-models').onclick = fetchModels;
    $('#dataset').onchange = function () { updateDatasetHint(); saveForm(); };
    FIELDS.forEach(function (f) {
      var el = $('#' + f);
      if (el && el.type !== 'hidden') el.addEventListener('change', saveForm);
    });

    try {
      var meta = await (await fetch('/api/meta')).json();
      DATASET_INFO = meta.dataset_info || [];
      var ds = $('#dataset');
      ds.innerHTML = (meta.datasets || []).map(function (n) {
        return '<option value="' + esc(n) + '">' + esc(n) + '</option>';
      }).join('');
      updateDatasetHint();
    } catch (e) {
      console.error(e);
    }
    loadReports();
  }

  boot();
})();
