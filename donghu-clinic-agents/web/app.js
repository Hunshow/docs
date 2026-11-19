/* ============================================================================
   东湖门诊 · MVP 前端交互逻辑
   原则：所有数据都走本机 FastAPI 接口，前端不做任何业务判定（判定归后端与人工）
   统一响应结构：{ ok: bool, data: any, error: { code, msg } }
   ========================================================================== */

/* ---------------- 全局状态 ---------------- */
const S = {
  token: null,        // 登录令牌（仅存内存 + sessionStorage，关浏览器即失效）
  role: null,         // 角色：patient / nurse / dept_staff / admin
  username: null,     // 账号
  display: null,      // 展示名
  recommendId: null,  // 当前待人工确认的推荐编号
};

/* 角色 → 页签配置：页签 id 与面板 id 一一对应 */
const TABS = {
  patient: [
    { id: "patient-book", label: "预约挂号" },
    { id: "queue-check", label: "候诊进度" },
  ],
  nurse: [
    { id: "verify", label: "核验与分诊推荐" },
    { id: "queue", label: "叫号队列" },
    { id: "notify", label: "协同提醒" },
    { id: "queue-check", label: "候诊进度" },
  ],
  dept_staff: [
    { id: "queue", label: "我的队列" },
    { id: "forecast", label: "流量预判" },
    { id: "notify", label: "异常反馈" },
  ],
  admin: [
    { id: "admin", label: "统计与审计" },
    { id: "queue", label: "队列总览" },
    { id: "forecast", label: "流量预判" },
  ],
};

/* 角色中文名 */
const ROLE_CN = { patient: "患者", nurse: "分诊/护士", dept_staff: "科室医护", admin: "管理员" };

/* ---------------- 基础工具 ---------------- */

/** 调用本机 API。入参 path 相对 /api 的路径、method、body；出参解析后的统一结构。
 *  失败行为：网络异常返回 {ok:false,error:{code:"NETWORK"}}；401 自动登出。 */
async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };                       // 组装请求参数
  if (S.token) {                                             // 已登录
    // 关键：同时用自定义头传令牌，而不是只依赖 Authorization
    // 部署到线上网关后实测：网关会把 Authorization 整个替换成它自己的 JWT，原始令牌直接丢失，
    // 表现就是“登录成功、所有业务接口 401”；而自定义头 X-Auth-Token 会被原样透传。
    // 两个头都带，本地直连与线上代理两种环境都能用，后端任一通道验签通过即可。
    opt.headers["X-Auth-Token"] = S.token;                   // 代理环境下可用的通道
    opt.headers["Authorization"] = "Bearer " + S.token;      // 标准通道（本地直连时使用）
  }
  if (body !== null) {                                       // 有请求体
    opt.headers["Content-Type"] = "application/json";        // JSON 内容类型
    opt.body = JSON.stringify(body);                         // 序列化
  }
  try {
    const r = await fetch("/api" + path, opt);               // 发起请求（同源，无跨域）
    const j = await r.json();                                // 解析统一结构
    if (j && j.ok === false && j.error && j.error.code === "AUTH_REQUIRED") logout();  // 令牌失效则登出
    return j;                                                // 返回结构
  } catch (e) {
    return { ok: false, data: null, error: { code: "NETWORK", msg: "无法连接本地服务：" + e.message } };
  }
}

/** 简单 HTML 转义，防止数据里的尖括号破坏页面结构。入参任意值；出参字符串。 */
function esc(v) {
  if (v === null || v === undefined) return "-";             // 空值统一显示短横
  return String(v).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/** 底部轻提示。入参文本；出参无；失败行为：无。 */
let toastTimer = null;                                       // 定时器句柄
function toast(msg) {
  const el = document.getElementById("toast");               // 取提示条
  el.textContent = msg;                                      // 设置文案
  el.hidden = false;                                         // 显示
  clearTimeout(toastTimer);                                  // 清掉上一个定时器
  toastTimer = setTimeout(() => { el.hidden = true; }, 2200); // 2.2 秒后隐藏
}

/** 渲染表格。入参 tableId、列定义 [{key,label,render?}]、数据数组；出参无。 */
function renderTable(tableId, cols, rows) {
  const t = document.getElementById(tableId);                // 取表格
  t.querySelector("thead").innerHTML =
    "<tr>" + cols.map(c => `<th>${esc(c.label)}</th>`).join("") + "</tr>";  // 表头
  if (!rows || rows.length === 0) {                          // 空数据
    t.querySelector("tbody").innerHTML =
      `<tr><td colspan="${cols.length}" style="color:#6b7785">暂无数据</td></tr>`;  // 占位行
    return;                                                  // 结束
  }
  t.querySelector("tbody").innerHTML = rows.map(r =>
    "<tr>" + cols.map(c => `<td>${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("") + "</tr>"
  ).join("");                                                // 行拼接
}

/** 结果块渲染：把键值结构输出为可读区块。入参容器 id、HTML 片段；出参无。 */
function setResult(id, html) { document.getElementById(id).innerHTML = html; }

/** 渲染推荐/核验结果的通用卡片。入参 agent 返回的 data；出参 HTML 字符串。 */
function agentCard(d) {
  if (!d) return "无返回数据";                                // 兜底
  const esc2 = esc;                                          // 别名，便于阅读
  let h = "";                                                // 累积 HTML
  h += `<div class="kv"><span class="k">摘要</span><span>${esc2(d.summary)}</span></div>`;  // 摘要
  if (d.escalation)                                          // 有升级说明
    h += `<div class="kv"><span class="k">转人工</span><span class="badge warn">${esc2(d.escalation)}</span></div>`;
  const need = d.requires_human_confirm ?                            // 是否需要人工
    '<span class="badge warn">需人工确认</span>' :
    '<span class="badge ok">无需人工终审</span>';
  h += `<div class="kv"><span class="k">人工环节</span><span>${need}</span></div>`;  // 人工环节标识
  if (Array.isArray(d.findings) && d.findings.length) {      // 核验发现
    h += '<div class="kv"><span class="k">核验发现</span><span></span></div>';        // 小标题
    h += "<ul>" + d.findings.map(f => `<li>${esc2(f.check)}：${esc2(f.result)}</li>`).join("") + "</ul>";
  }
  if (Array.isArray(d.references) && d.references.length) {  // RAG 事实引用
    h += '<div class="kv"><span class="k">事实引用</span><span></span></div>';
    h += "<ul>" + d.references.slice(0, 4).map(r =>
      `<li>${esc2(r.doc || r.source || "知识库")}：${esc2((r.text || "").slice(0, 80))}…</li>`).join("") + "</ul>";
  }
  if (d.data_version)                                        // 资料版本
    h += `<div class="kv"><span class="k">资料版本</span><span>${esc2(d.data_version)}</span></div>`;
  const mode = d.mode || d.reasoning_mode || "-";            // 推理模式（llm / rules）
  h += `<div class="kv"><span class="k">推理模式</span><span class="badge info">${esc2(mode)}</span></div>`;
  return h;                                                  // 返回片段
}

/* ---------------- 登录 / 登出 ---------------- */

/** 执行登录。入参 username、password；出参无（失败在界面提示）。 */
async function doLogin(username, password) {
  const j = await api("/auth/login", "POST", { username, password });  // 调登录接口
  const msg = document.getElementById("login-msg");                   // 提示位
  if (!j.ok) {                                                       // 失败
    msg.className = "msg err";                                       // 红色
    msg.textContent = j.error.msg || "登录失败";                      // 文案
    return;                                                          // 结束
  }
  S.token = j.data.token;                                            // 存令牌
  S.role = j.data.role;                                              // 存角色
  S.username = j.data.username;                                      // 存账号
  S.display = j.data.display_name;                                   // 存展示名
  sessionStorage.setItem("mvp_token", S.token);                      // 会话级持久化（刷新不丢）
  sessionStorage.setItem("mvp_user", JSON.stringify(j.data));        // 存身份
  enterApp();                                                        // 进入业务界面
}

/** 进入业务界面：渲染身份、页签、默认面板。入参无；出参无。 */
function enterApp() {
  document.getElementById("view-login").hidden = true;               // 隐藏登录
  document.getElementById("view-app").hidden = false;               // 显示业务
  document.getElementById("who-text").textContent =
    `${S.display}（${ROLE_CN[S.role] || S.role}）`;                    // 顶部身份
  document.getElementById("btn-logout").hidden = false;             // 显示退出
  const tabs = TABS[S.role] || [];                                  // 当前角色页签
  const nav = document.getElementById("tabs");                      // 页签容器
  nav.innerHTML = tabs.map((t, i) =>
    `<button class="tab${i === 0 ? " active" : ""}" data-panel="${t.id}">${esc(t.label)}</button>`).join("");  // 渲染页签
  nav.querySelectorAll(".tab").forEach(b => {                        // 绑定点击
    b.onclick = () => switchPanel(b.dataset.panel, b);               // 切换
  });
  if (tabs.length) switchPanel(tabs[0].id, nav.querySelector(".tab"));  // 默认第一个
  if (S.role === "patient") prepBookForm();                         // 患者预填预约表单
}

/** 切换面板。入参 panelId、触发按钮；出参无。 */
function switchPanel(panelId, btn) {
  document.querySelectorAll(".panel").forEach(p => { p.hidden = true; });  // 全部隐藏
  const p = document.getElementById("panel-" + panelId);             // 目标面板
  if (p) p.hidden = false;                                           // 显示目标
  document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));  // 清激活态
  if (btn) btn.classList.add("active");                              // 设置激活态
  if (panelId === "queue") loadQueue();                              // 队列自动加载
  if (panelId === "admin") { loadStats(); }                          // 统计自动加载
}

/** 退出登录：清空状态并回到登录页。入参无；出参无。 */
let loggingOut = false;                                     // 防重入标志：避免 401 触发登出时反复调用登出接口
function logout() {
  if (!loggingOut) {                                        // 未在登出中
    loggingOut = true;                                      // 置位，阻断递归
    // 通知后端删除 HttpOnly 令牌 Cookie：前端读不到也删不掉它，必须由服务端下发删除指令
    api("/auth/logout", "POST").finally(() => { loggingOut = false; });  // 失败也不阻塞本地登出
  }
  S.token = null; S.role = null; S.username = null; S.display = null; S.recommendId = null;
  sessionStorage.clear();                                            // 清会话
  document.getElementById("view-app").hidden = true;                 // 隐藏业务
  document.getElementById("view-login").hidden = false;             // 显示登录
  document.getElementById("btn-logout").hidden = true;               // 隐藏退出
  document.getElementById("who-text").textContent = "未登录";        // 复位身份
}

/* ---------------- 患者 ---------------- */

/** 预填预约表单：患者姓名默认为登录账号展示名、时段默认「今天 09:00」，减少演示时手输。入参无；出参无。 */
function prepBookForm() {
  const nm = document.getElementById("bk-name");                     // 患者姓名输入
  if (nm && !nm.value && S.display) nm.value = S.display;            // 未填时预填登录展示名——与"我的预约"口径一致
  const el = document.getElementById("bk-time");                     // 时段输入
  if (!el.value) {                                                   // 未填时
    const d = new Date();                                            // 当前时间
    const pad = n => String(n).padStart(2, "0");                     // 补零
    el.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T09:00`;  // 格式化为 datetime-local
  }
}

/** 提交预约。入参无（读表单）；出参无（结果写提示位）。 */
async function book() {
  const payload = {                                                  // 组装请求体
    patient_name: document.getElementById("bk-name").value.trim(),
    phone: document.getElementById("bk-phone").value.trim(),
    medical_no: document.getElementById("bk-medical").value.trim(),
    dept: document.getElementById("bk-dept").value,
    doctor: document.getElementById("bk-doctor").value,
    schedule_time: (document.getElementById("bk-time").value || "").replace("T", " "),
  };
  const j = await api("/patient/appointments", "POST", payload);     // 调接口
  const m = document.getElementById("book-msg");                     // 提示位
  if (!j.ok) {                                                       // 失败
    m.className = "msg err"; m.textContent = j.error.msg; return;     // 显示错误
  }
  m.className = "msg ok";                                            // 成功样式
  m.textContent = `预约成功：${j.data.apt_id}（${j.data.dept} / ${j.data.doctor} / ${j.data.schedule_time}）`;  // 回显（已脱敏）
  loadMine();                                                        // 刷新列表
}

/** 加载我的预约。入参无；出参无。 */
async function loadMine() {
  const j = await api("/patient/appointments/mine");                 // 调接口
  if (!j.ok) { toast(j.error.msg); return; }                         // 失败提示
  renderTable("tbl-my", [
    { key: "apt_id", label: "预约号" },
    { key: "patient_name", label: "姓名" },
    { key: "phone", label: "手机号" },
    { key: "medical_no", label: "病历号" },
    { key: "dept", label: "科室" },
    { key: "doctor", label: "医生" },
    { key: "schedule_time", label: "时段" },
    { key: "status", label: "状态" },
  ], j.data);                                                        // 渲染（字段已脱敏）
}

/** 查询候诊进度。入参无；出参无。 */
async function checkQueue() {
  const apt = document.getElementById("qc-apt").value.trim();        // 预约号
  const j = await api("/patient/queue/position/" + encodeURIComponent(apt));  // 调接口
  if (!j.ok) { setResult("qc-out", `<span class="badge danger">${esc(j.error.msg)}</span>`); return; }
  const d = j.data;                                                  // 结果
  setResult("qc-out",
    `<div class="kv"><span class="k">预约号</span><span>${esc(d.apt_id)}</span></div>` +
    `<div class="kv"><span class="k">当前位次</span><span>${d.position ? "第 " + d.position + " 位" : '<span class="badge warn">不在队列</span>'}</span></div>` +
    `<div class="kv"><span class="k">口径说明</span><span>${esc(d.notice)}</span></div>`);
}

/* ---------------- 队列 ---------------- */

/** 加载队列。入参无；出参无。 */
async function loadQueue() {
  const j = await api("/staff/queue");                               // 调接口
  if (!j.ok) { toast(j.error.msg); return; }                         // 失败提示
  renderTable("tbl-queue", [
    { key: "position", label: "位次" },
    { key: "apt_id", label: "预约号" },
    { key: "dept", label: "科室" },
    { key: "doctor", label: "医生" },
    { key: "check_in_time", label: "签到时间" },
    {
      key: "is_late_or_walk_in", label: "混队标注",                    // 迟到/加号标注
      render: r => r.is_late_or_walk_in
        ? '<span class="badge warn">迟到/加号</span>'
        : '<span class="badge ok">正常</span>',
    },
  ], j.data);
}

/* ---------------- 护士：核验 / 分诊推荐 ---------------- */

/** 执行预约核验。入参无；出参无。 */
async function verify() {
  const apt = document.getElementById("vf-apt").value.trim();        // 预约号
  const j = await api("/agents/appointment_verifier/run", "POST", { query: "核验预约", apt_id: apt });  // 调智能体
  if (!j.ok) { setResult("vf-out", `<span class="badge danger">${esc(j.error.msg)}</span>`); return; }
  setResult("vf-out", agentCard(j.data));                            // 渲染结果
}

/** 生成分诊推荐。入参无；出参无。 */
async function triage() {
  const apt = document.getElementById("tr-apt").value.trim();        // 预约号
  const j = await api("/agents/triage_advisor/run", "POST", { query: "生成分诊推荐", apt_id: apt });  // 调智能体
  if (!j.ok) { setResult("tr-out", `<span class="badge danger">${esc(j.error.msg)}</span>`); return; }
  const d = j.data;                                                  // 结果
  setResult("tr-out", agentCard(d));                                 // 渲染
  const hasRec = !!(d.recommendation && d.recommend_id);              // 是否产出可用推荐
  S.recommendId = hasRec ? d.recommend_id : null;                     // 记住推荐号
  document.getElementById("tr-actions").hidden = !hasRec;             // 无推荐时不显示确认按钮
  if (hasRec) toast("推荐已生成，状态：待人工确认");                   // 提示
}

/** 人工确认 / 驳回推荐。入参 confirm 布尔；出参无。 */
async function confirmRec(confirm) {
  if (!S.recommendId) { toast("请先生成推荐"); return; }              // 前置校验
  const note = document.getElementById("tr-note").value.trim();       // 人工理由
  if (!note) { toast("必须填写人工改动/驳回理由"); return; }            // 质控要求：无理由不得提交
  const j = await api("/agents/recommend/" + encodeURIComponent(S.recommendId) + "/confirm",
    "POST", { confirm, note });                                       // 提交确认
  if (!j.ok) { toast(j.error.msg); return; }                          // 失败提示
  const d = j.data;                                                   // 结果
  toast(confirm ? "已人工确认（留痕完成）" : "已驳回（留痕完成）");      // 提示
  setResult("tr-out",
    `<div class="kv"><span class="k">推荐编号</span><span>${esc(d.recommend_id)}</span></div>` +
    `<div class="kv"><span class="k">状态</span><span class="badge ${confirm ? "ok" : "danger"}">${esc(d.status)}</span></div>` +
    `<div class="kv"><span class="k">签核人</span><span>${esc(d.confirmed_by)}</span></div>` +
    `<div class="kv"><span class="k">人工说明</span><span>${esc(d.human_note)}</span></div>` +
    `<div class="kv"><span class="k">确认时间</span><span>${esc(d.confirm_time)}</span></div>` +
    `<div class="kv"><span class="k">资料版本</span><span>${esc(d.data_version)}</span></div>`);
  document.getElementById("tr-actions").hidden = true;                // 收起操作区
  S.recommendId = null;                                               // 清空状态
}

/* ---------------- 协同提醒 / 异常反馈 ---------------- */

/** 生成通知。入参无；出参无。 */
async function notify() {
  const payload = {                                                   // 组装任务
    query: "生成协同提醒",
    apt_id: document.getElementById("nt-apt").value.trim(),
    notify_type: document.getElementById("nt-type").value,
    reason: document.getElementById("nt-reason").value.trim(),
    receiver: document.getElementById("nt-receiver").value.trim(),
  };
  // 科室医护没有 send_notify 权限，走反馈异常接口；护士走智能体
  const j = S.role === "dept_staff"
    ? await api("/staff/feedback", "POST", { apt_id: payload.apt_id, reason: payload.reason })  // 异常反馈
    : await api("/agents/collab_notifier/run", "POST", payload);       // 协同提醒智能体
  if (!j.ok) { setResult("nt-out", `<span class="badge danger">${esc(j.error.msg)}</span>`); return; }
  const d = j.data;                                                   // 结果
  if (d.message !== undefined) {                                      // 智能体返回结构
    setResult("nt-out",
      `<div class="kv"><span class="k">面向对象</span><span class="badge info">${esc(d.audience)}</span></div>` +
      `<div class="kv"><span class="k">文案</span><span>${esc(d.message)}</span></div>` +
      `<div class="kv"><span class="k">发送约束</span><span>${d.requires_human_confirm ?
        '<span class="badge warn">需人工确认后发送</span>' : '<span class="badge ok">可直接发送</span>'}</span></div>`);
  } else {                                                            // 异常反馈返回结构
    setResult("nt-out",
      `<div class="kv"><span class="k">反馈编号</span><span>${esc(d.feedback_id)}</span></div>` +
      `<div class="kv"><span class="k">状态</span><span class="badge warn">${esc(d.status)}</span></div>` +
      `<div class="kv"><span class="k">说明</span><span>${esc(d.note)}</span></div>`);
  }
}

/* ---------------- 流量预判 ---------------- */

/** 执行流量预判。入参无；出参无。 */
async function forecast() {
  const j = await api("/staff/forecast");                             // 调接口
  if (!j.ok) { setResult("fc-out", `<span class="badge danger">${esc(j.error.msg)}</span>`); return; }
  const d = j.data;                                                   // 结果
  setResult("fc-out",
    `<div class="kv"><span class="k">预判摘要</span><span>${esc(d.summary)}</span></div>` +
    `<div class="kv"><span class="k">预判时段</span><span>${esc(d.peak_hour)} 时</span></div>` +
    `<div class="kv"><span class="k">预计人次</span><span>${esc(d.expected_count)}</span></div>` +
    `<div class="kv"><span class="k">当前在队</span><span>${esc(d.queue_size)}</span></div>` +
    `<div class="kv"><span class="k">预警</span><span>${d.warning ?
      '<span class="badge warn">' + esc(d.warning) + "</span>" :
      '<span class="badge ok">无异常</span>'}</span></div>` +
    `<div class="kv"><span class="k">资料版本</span><span>${esc(d.data_version)}</span></div>`);
}

/* ---------------- 管理员：统计 / 审计 ---------------- */

/** 加载统计。入参无；出参无。 */
async function loadStats() {
  const j = await api("/admin/stats");                                // 调接口
  if (!j.ok) { toast(j.error.msg); return; }                          // 失败提示
  const d = j.data;                                                   // 数据
  const items = [                                                     // 指标卡定义
    ["预约总量", d.appointment_total, ""],
    ["爽约量", d.no_show_total, "warn"],
    ["迟到/加号", d.late_or_walk_in_total, "warn"],
    ["待人工确认", d.recommend_pending, "warn"],
    ["已确认", d.recommend_confirmed, "ok"],
    ["已驳回", d.recommend_rejected, "danger"],
  ];
  document.getElementById("st-cards").innerHTML = items.map(([t, n, cls]) =>
    `<div class="stat"><div class="n ${cls}">${esc(n)}</div><div class="t">${esc(t)}</div></div>`).join("") +
    `<div class="stat"><div class="n" style="font-size:14px">${esc(d.data_version)}</div><div class="t">资料版本</div></div>`;  // 版本卡
}

/** 调阅审计。入参无；出参无。 */
async function loadAudit() {
  const biz = document.getElementById("au-biz").value.trim();          // 业务编号过滤
  const q = biz ? "?biz_id=" + encodeURIComponent(biz) + "&limit=80" : "?limit=80";  // 查询串
  const j = await api("/admin/audit" + q);                             // 调接口
  if (!j.ok) { toast(j.error.msg); return; }                           // 失败提示
  renderTable("tbl-audit", [
    { key: "ts", label: "时间" },
    { key: "actor", label: "操作人" },
    { key: "role", label: "角色" },
    { key: "action", label: "动作" },
    { key: "biz_id", label: "业务编号" },
    { key: "data_version", label: "资料版本" },
    { key: "decision", label: "结论", render: r => {
        const m = { PENDING_HUMAN: "warn", HUMAN_CONFIRMED: "ok", REJECTED: "danger", DENIED: "danger" };  // 结论色
        const c = m[r.decision] || "info";                             // 默认色
        return `<span class="badge ${c}">${esc(r.decision)}</span>`;    // 徽标
      } },
  ], j.data);
}

/* ---------------- 事件绑定 ---------------- */

/** 页面初始化：绑定全部交互事件并尝试恢复会话。入参无；出参无。 */
function boot() {
  document.getElementById("btn-login").onclick = () =>                 // 登录按钮
    doLogin(document.getElementById("login-user").value.trim(),
            document.getElementById("login-pass").value);              // 传表单值
  document.querySelectorAll(".quick-btn").forEach(b => {               // 四个快捷登录
    b.onclick = () => doLogin(b.dataset.u, b.dataset.p);               // 用预置口令
  });
  document.getElementById("btn-logout").onclick = logout;              // 退出

  document.getElementById("btn-book").onclick = book;                  // 提交预约
  document.getElementById("btn-my").onclick = loadMine;                // 刷新我的预约
  document.getElementById("btn-qc").onclick = checkQueue;              // 查候诊进度
  document.getElementById("btn-queue").onclick = loadQueue;            // 刷新队列
  document.getElementById("btn-verify").onclick = verify;              // 执行核验
  document.getElementById("btn-triage").onclick = triage;              // 生成推荐
  document.getElementById("btn-confirm").onclick = () => confirmRec(true);   // 确认
  document.getElementById("btn-reject").onclick = () => confirmRec(false);   // 驳回
  document.getElementById("btn-notify").onclick = notify;              // 生成通知
  document.getElementById("btn-forecast").onclick = forecast;          // 流量预判
  document.getElementById("btn-stats").onclick = loadStats;            // 刷新统计
  document.getElementById("btn-audit").onclick = loadAudit;            // 调阅审计

  const saved = sessionStorage.getItem("mvp_token");                   // 尝试恢复会话
  if (saved) {                                                         // 有历史令牌
    const u = JSON.parse(sessionStorage.getItem("mvp_user") || "{}");   // 恢复身份
    S.token = saved; S.role = u.role; S.username = u.username; S.display = u.display_name;  // 回填状态
    enterApp();                                                        // 直接进入
  }
}

boot();  // 启动
