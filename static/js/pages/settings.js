import { api, escapeHtml } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  const items = data.items || [];
  const snapshot = data.snapshot || {};

  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <p class="muted">控制质控自动扫描开关、补采最大次数、配额告警与对账口径标签等。修改会写审计。</p>
      <div class="grid">
        <div class="stat"><div class="n">${snapshot.backfill_max_attempt || '-'}</div><div class="l">补采最大次数</div></div>
        <div class="stat"><div class="n">${snapshot.qc_auto_scan || '-'}</div><div class="l">自动质控</div></div>
        <div class="stat"><div class="n">${snapshot.site_quota_warn || '-'}</div><div class="l">配额告警阈值</div></div>
        <div class="stat"><div class="n">${escapeHtml((snapshot.clinic_timezone || '-').slice(0, 12))}</div><div class="l">时区</div></div>
      </div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>键</th><th>说明</th><th>当前值</th><th>新值</th><th>操作</th></tr></thead>
        <tbody>
          ${items.map((s) => `
            <tr>
              <td><code>${escapeHtml(s.key)}</code></td>
              <td>${escapeHtml(s.label)}</td>
              <td><strong>${escapeHtml(s.value)}</strong></td>
              <td><input data-key="${escapeHtml(s.key)}" value="${escapeHtml(s.value)}" /></td>
              <td><button data-save="${escapeHtml(s.key)}">保存</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div id="msg" class="muted" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h2>预留切口（后续题面）</h2>
      <ul class="muted">
        <li>访视行跨中心去重策略与冲突裁决面板</li>
        <li>方案换版后存量批次字段兼容扫描</li>
        <li>补采任务分片与并发上限</li>
        <li>对账报告导出与差异工单</li>
      </ul>
      <div class="detail-box">${escapeHtml(JSON.stringify(snapshot, null, 2))}</div>
    </div>`;

  const msg = el.querySelector('#msg');
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.save;
      const input = el.querySelector(`input[data-key="${key}"]`);
      try {
        const updated = await api('/api/settings/update', {
          method: 'POST',
          body: JSON.stringify({ key, value: input.value }),
        });
        msg.className = 'muted';
        msg.textContent = `已保存 ${updated.key} = ${updated.value}`;
        await renderSettings(el);
      } catch (e) {
        msg.className = 'err';
        msg.textContent = e.message;
      }
    };
  });

  try {
    const dash = await api('/api/collision_gate/dash');
    el.innerHTML += `<div class="card" style="margin-top:1rem">
      <h3>撞键钉定（甲路·物化包）</h3>
      <span class="chip ${dash.aligned ? 'ok' : 'bad'}">${dash.chip} / ${dash.truth}</span>
      <p class="muted">读轨 ${dash.active_read} · 指纹 ${(dash.fingerprint || '空').slice(0, 16)} · 拒回刷 ${dash.reject_refresh} · 镜像次数 ${dash.mirror_count}／钉定 ${dash.pinned_mirror_count} · 落点行 ${dash.landing_row ?? '无'} · 镜像拦截 ${dash.mirror_blocked}</p>
      <div class="row">
        <button id="go_collision_gate" type="button">撞键钉定</button>
        <button id="cut_collision_gate" type="button">切流账册</button>
      </div>
      <div id="gateMsg" class="muted" style="margin-top:8px"></div>
    </div>`;
    const gateMsg = el.querySelector('#gateMsg');
    const go = el.querySelector('#go_collision_gate');
    if (go) go.onclick = async () => {
      try {
        await api('/api/collision_gate/commit', { method:'POST', body: JSON.stringify({ref_id:1, note:'台参数页钉定', expect_pin_id: dash.landing_row || 0}) });
        await renderSettings(el);
      } catch (e) {
        if (gateMsg) { gateMsg.className = 'err'; gateMsg.textContent = e.message; }
      }
    };
    const cut = el.querySelector('#cut_collision_gate');
    if (cut) cut.onclick = async () => {
      try {
        await api('/api/collision_gate/cutover', { method:'POST', body: JSON.stringify({ref_id:1, note:'撞键闸切流', expect_pin_id: dash.landing_row || 0}) });
        await renderSettings(el);
      } catch (e) {
        if (gateMsg) { gateMsg.className = 'err'; gateMsg.textContent = e.message; }
      }
    };
  } catch (e) { /* optional surface */ }

}
