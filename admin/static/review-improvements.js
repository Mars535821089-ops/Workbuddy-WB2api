/* Authenticated maintenance and provenance-aware credit history. */
window.WBReview = (() => {
  let page = 1, generation = 0;
  async function json(path, options) {
    const result = await safeJson(await api(path, options));
    if (!result.ok) throw new Error(typeof result.data.detail === 'string' ? result.data.detail : 'HTTP ' + result.status);
    return result.data;
  }
  function options(id, rows, label) {
    const select = document.getElementById(id), selected = select.value;
    select.innerHTML = '<option value="">' + label + '</option>' + rows.map(([value, text]) => '<option value="' + esc(String(value)) + '">' + esc(String(text)) + '</option>').join('');
    select.value = selected;
  }
  async function loadJournal(next = 1) {
    const mine = ++generation;
    const query = new URLSearchParams({page: next, page_size: 50});
    for (const [id, name] of [['journalAccount','account_id'],['journalSource','source'],['journalKind','kind']]) {
      const value = document.getElementById(id).value; if (value) query.set(name, value);
    }
    for (const [id, name] of [['journalStart','start'],['journalEnd','end']]) {
      const value = document.getElementById(id).value; if (value) query.set(name, new Date(value).toISOString());
    }
    document.getElementById('journalHint').textContent = '正在读取积分流水…';
    try {
      const data = await json('/api/maintenance/credit-events?' + query);
      if (mine !== generation) return;
      page = data.page;
      options('journalAccount', data.accounts.map(a => [a.id, a.name || '#' + a.id]), '全部账号');
      options('journalSource', data.sources.map(s => [s,s]), '全部来源');
      document.getElementById('journalBody').innerHTML = data.items.map(row => '<tr><td>' + esc(new Date(row.created_at).toLocaleString()) + '</td><td>' + esc(row.account_name || '#' + row.account_id) + ' <small>#' + row.account_id + '</small></td><td>' + (row.kind === 'confirmed' ? '确认奖励' : '余额观察') + '</td><td>' + esc(row.source) + '</td><td>' + (row.delta > 0 ? '+' : '') + Number(row.delta) + '</td><td>' + (row.balance_after == null ? '未记录' : Number(row.balance_after)) + '</td></tr>').join('') || '<tr><td colspan="6">没有符合条件的记录</td></tr>';
      document.getElementById('journalHint').textContent = '共 ' + data.total + ' 条。余额观察的来源未确认，可能包含消费、奖励、到期或对账修正。' + (data.importing ? '历史记录分批导入中，请刷新继续。' : '');
      document.getElementById('journalPage').textContent = '第 ' + page + ' / ' + Math.max(1,Math.ceil(data.total / data.page_size)) + ' 页';
      document.getElementById('journalPrev').disabled = page <= 1;
      document.getElementById('journalNext').disabled = page * data.page_size >= data.total;
    } catch (error) { if (mine === generation) document.getElementById('journalHint').textContent = error.message; }
  }
  async function loadUpdate() {
    const panel = document.getElementById('maintenanceInfo');
    panel.textContent = '正在检查正式版本…';
    try {
      const data = await json('/api/maintenance/update');
      panel.innerHTML = '<h3>当前版本 ' + esc(data.version) + ' · ' + (data.latest ? '最新正式版 ' + esc(data.latest.tag) : '未获取到正式版本') + '</h3><p class="my-3">' + esc(data.error || (data.available ? '有新版本可升级' : '未发现更高版本')) + '</p><p>' + esc(data.backup_note) + '</p><div class="flex gap-2 my-3"><button class="console-btn" onclick="WBReview.backup()" ' + (data.backup_supported ? '' : 'disabled') + '>下载 SQLite 数据库备份</button><a href="' + esc(data.releases_url) + '" target="_blank" rel="noopener noreferrer">查看正式版本与公告</a></div>' + (data.mysql_backup_command ? '<p>当前使用 ' + esc(data.database) + '，请先使用数据库管理工具备份。</p><pre class="overflow-x-auto p-3">' + esc(data.mysql_backup_command) + '</pre>' : '') + (data.commands ? Object.entries(data.commands).map(([name, command]) => '<details class="my-3"><summary>' + (name === 'docker' ? 'Docker Compose 升级命令' : 'Linux 源码升级命令') + '</summary><pre class="overflow-x-auto p-3">' + esc(command) + '</pre></details>').join('') : '');
    } catch (error) { panel.textContent = error.message; }
  }
  async function backup() {
    try {
      const response = await api('/api/maintenance/backup', {method:'POST'});
      if (!response.ok) { const result = await safeJson(response); throw new Error(result.data.detail || 'HTTP ' + response.status); }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = 'workbuddy-backup-' + new Date().toISOString().slice(0,10) + '.db';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),60000);
    } catch (error) { toast(error.message,'err'); }
  }
  function accountLimit(id) {
    const account = accById(id);
    openModal('<h3>账号并发上限 · ' + esc(account?.name || String(id)) + '</h3><p class="my-3">留空跟随全局；0 表示不限；正整数限制同时请求数。只影响后续请求，维护中的账号仍会暂停接入。</p><input id="accountInFlight" type="number" min="0" step="1" max="2147483647" value="' + (account?.max_in_flight ?? '') + '" placeholder="留空跟随全局" aria-label="账号并发上限" /><button class="console-btn" onclick="WBReview.saveLimit(' + id + ')">保存</button>', 'max-w-lg');
  }
  async function saveLimit(id) {
    const raw = document.getElementById('accountInFlight').value;
    const value = raw === '' ? null : Number(raw);
    if (value !== null && (!Number.isInteger(value) || value < 0 || value > 2147483647)) return toast('请输入非负整数或留空','err');
    try { await json('/api/accounts/' + id, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_in_flight:value})}); closeModal(); loadAccounts(); } catch (error) { toast(error.message,'err'); }
  }
  return {loadJournal, journalPage:delta => loadJournal(Math.max(1,page+delta)), loadUpdate, backup, accountLimit, saveLimit};
})();
document.addEventListener('DOMContentLoaded', () => {
  if (document.documentElement.dataset.activeTab === 'journal') WBReview.loadJournal(1);
  if (document.documentElement.dataset.activeTab === 'maintenance') WBReview.loadUpdate();
});
