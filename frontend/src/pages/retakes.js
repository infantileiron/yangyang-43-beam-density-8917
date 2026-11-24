import { get, post } from '../api/client.js';
import { table } from '../components/common/data.js';
import { intro } from '../components/common/frame.js';
import { formError } from '../components/common/forms.js';
import { OrderStatus, Role, orderStatusName } from '../constants/enums.js';
import { bindForm, notify } from '../hooks/actions.js';
import { esc, timeLabel } from '../utils/html.js';

function orderBadge(order, activeId) {
  const name = orderStatusName[order.status] || order.status;
  const cls = order.id === activeId ? 'status-approved' : order.status === OrderStatus.DISCARDED ? 'status-returned' : 'status-queued';
  return `<span class="status ${cls}">${esc(name)}</span>`;
}

function orderCard(order, activeId, writable) {
  const registered = new Set(order.registrations.map(item => item.ticket));
  const rows = order.tickets.map(ticket => ({
    ticket,
    done: registered.has(ticket),
    note: order.registrations.find(item => item.ticket === ticket)?.note || '',
    actor: order.registrations.find(item => item.ticket === ticket)?.actor || '',
  }));
  const canActivate = writable && order.status === OrderStatus.COMPUTING;
  const canRegister = writable && order.id === activeId;
  return `<section class="panel retake-order" data-order="${order.id}">
    <div class="panel-heading"><h2>复拍清单 #${order.id} <small>批次序号 ${order.sequence}</small></h2>
      <span>${orderBadge(order, activeId)} <a class="text-link" href="#result/${order.run_id}">来源分析 #${order.run_id}</a></span></div>
    <div class="panel-body">
      ${canActivate ? `<form class="activate-form" data-order="${order.id}"><button class="primary">接替生效</button>${formError}</form>` : ''}
      ${order.activated_at ? `<p class="aside-note">生效时间：${timeLabel(order.activated_at)}</p>` : ''}
      ${table([
        { key: 'ticket', label: '拍摄编号' },
        { label: '复拍场次', html: row => row.done ? '<span class="status status-approved">已登记</span>' : '<span class="status status-queued">待复拍</span>' },
        { key: 'note', label: '登记备注' },
        { key: 'actor', label: '登记人' },
      ], rows, '这份清单没有待复拍拍摄。')}
      ${canRegister && rows.length ? `<details class="register-box"><summary>登记复拍场次</summary><form class="register-form" data-order="${order.id}">
        <label class="field"><span>拍摄编号</span><select name="ticket" required>${rows.filter(row => !row.done)
          .map(row => `<option value="${esc(row.ticket)}">${esc(row.ticket)}</option>`).join('')}</select></label>
        <label class="field"><span>备注</span><input name="note" maxlength="300" placeholder="当班复拍情况（选填）"></label>
        ${formError}<button class="primary">保存登记</button></form></details>` : ''}
    </div>
  </section>`;
}

export async function retakePage(spec, user) {
  const data = await get('/retakes/orders');
  const writable = user.role !== Role.VIEWER;
  const active = data.items.find(order => order.id === data.active_id);
  const head = intro('复拍清单', '同一拍摄编号按复拍批次号保留最新读数；新清单算成并接替后才生效，旧清单晚到不能换回。',
    active ? `<span class="count">生效清单 <small>#${active.id}</small></span>` : '');
  if (!data.items.length) {
    return `${head}<div class="empty">还没有复拍清单。分析算完后，孤点拍摄会自动汇总成清单。</div>`;
  }
  return `${head}
    ${active ? `<section class="panel"><div class="panel-heading"><h2>生效中的清单</h2></div>
      <p class="panel-body">清单 #${active.id}（序号 ${active.sequence}）含 ${active.tickets.length} 个待复拍拍摄，已登记 ${active.registrations.length} 场。同批重跑不会重复登记。</p></section>` : ''}
    ${data.items.map(order => orderCard(order, data.active_id, writable)).join('')}`;
}

export function wireRetakes(root, refresh) {
  root.querySelectorAll('.activate-form').forEach(form => {
    bindForm(form, async node => {
      await post(`/retakes/orders/${node.dataset.order}/activate`, {});
      notify('复拍清单已接替生效');
      refresh();
    });
  });
  root.querySelectorAll('.register-form').forEach(form => {
    bindForm(form, async node => {
      const payload = Object.fromEntries(new FormData(node));
      await post(`/retakes/orders/${node.dataset.order}/registrations`, payload);
      notify('复拍场次已登记');
      refresh();
    });
  });
}
