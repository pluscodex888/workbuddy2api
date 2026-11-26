const form = document.querySelector('#auth');
const input = document.querySelector('#token');
const info = document.querySelector('#message');
const panel = document.querySelector('#status');
const groups = document.querySelector('#groups');

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  panel.hidden = true;
  info.textContent = '正在读取状态…';
  try {
    const response = await fetch('/api/status', {
      headers: { Authorization: 'Bearer ' + input.value },
      cache: 'no-store',
      redirect: 'error',
    });
    if (response.status === 401) {
      info.textContent = '密钥无效。';
      return;
    }
    if (!response.ok) throw Error('Status unavailable');
    const data = await response.json();
    document.querySelector('#native').textContent = data.native_configured ? '已配置' : '待配置';
    document.querySelector('#dispatched').textContent = data.requests.dispatched ?? 0;
    document.querySelector('#rejected').textContent = data.requests.rejected ?? 0;
    document.querySelector('#errors').textContent = data.requests.upstream_error ?? 0;
    const rows = Object.entries(data.groups).map(([group, config]) => [group, config.models.join('、'), config.routes.join('、')]);
    groups.replaceChildren(...(rows.length ? rows : [['—', '尚未配置模型分组', '—']]).map(values => {
      const row = document.createElement('tr');
      for (const value of values) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      return row;
    }));
    input.value = '';
    info.textContent = '';
    panel.hidden = false;
  } catch {
    info.textContent = '暂时无法读取网关状态。';
  } finally {
    button.disabled = false;
  }
});
