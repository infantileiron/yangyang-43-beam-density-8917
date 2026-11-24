import { get, post } from '../api/client.js';
import { intro } from '../components/common/frame.js';
import { status, table } from '../components/common/data.js';
import { fieldControl, formError } from '../components/common/forms.js';
import { Role } from '../constants/enums.js';
import { bindForm } from '../hooks/actions.js';
import { formValues } from '../types/form.js';
import { esc, timeLabel } from '../utils/html.js';

export function runTable(runs, message) {
  return table([
    {label:'分析名称',html: row => `<a href="#result/${row.id}">${esc(row.title)}</a>`},
    {label:'观测条数',key:'sample_size'}, {label:'状态',html:status},
    {label:'提交时间',html:row => timeLabel(row.created_at)},
  ], runs, message);
}

export async function analysisPage(spec, user) {
  const runs = await get('/runs');
  return `${intro('分析任务', spec.algorithm)}<div class="split"><section class="panel"><div class="panel-heading"><h2>计算条件</h2></div>
    ${user.role === Role.ANALYST ? `<form id="run-form" class="panel-body"><label class="field"><span>分析名称</span><input name="title" required maxlength="120" value="${esc(spec.direction)}复核"></label><div class="form-grid">${spec.parameters.map(parameter => fieldControl(parameter,true)).join('')}</div>${formError}<button class="primary">提交计算</button></form>` : '<p class="panel-body">由业务分析员提交计算。此账号可查看当前角色有权访问的分析任务。</p>'}</section>
    <aside class="method"><h2>计算口径</h2><p>${esc(spec.formula)}</p><p class="aside-note">提交时按拍摄编号固化批次号最新的观测，排队期间到达的更正不影响本次分析；再次提交会一次取齐当时全部更正，并以上一份成功分析为基线识别新增孤点。</p><p><a class="text-link" href="#retakes">查看复拍清单 →</a></p></aside></div>
    <section class="panel"><div class="panel-heading"><h2>历史分析</h2></div>${runTable(runs, '还没有可查看的分析。提交计算后可在这里追踪。')}</section>`;
}

export function wireAnalysis(root, spec) {
  bindForm(root.querySelector('#run-form'), async form => {
    const run = await post('/runs', {title:form.elements.title.value,parameters:formValues(form,spec.parameters)});
    location.hash = `#result/${run.id}`;
  });
}
