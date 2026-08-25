const ui = {
  runSelect: document.querySelector("#run-select"),
  connection: document.querySelector("#connection-chip"),
  scenario: document.querySelector("#scenario-title"),
  runId: document.querySelector("#run-id"),
  status: document.querySelector("#run-status"),
  tick: document.querySelector("#metric-tick"),
  tasks: document.querySelector("#metric-tasks"),
  taskRate: document.querySelector("#metric-task-rate"),
  risk: document.querySelector("#metric-risk"),
  riskTrend: document.querySelector("#metric-risk-trend"),
  orders: document.querySelector("#metric-orders"),
  restrictions: document.querySelector("#metric-restrictions"),
  fleetMap: document.querySelector("#fleet-map"),
  operationsMap: document.querySelector("#operations-map"),
  roadLayer: document.querySelector("#road-layer"),
  trailLayer: document.querySelector("#trail-layer"),
  zoneLayer: document.querySelector("#zone-layer"),
  vehicleLayer: document.querySelector("#vehicle-layer"),
  vehicleList: document.querySelector("#vehicle-list"),
  riskDetail: document.querySelector("#risk-detail"),
  riskTrend: document.querySelector("#risk-trend"),
  boundary: document.querySelector("#boundary-text"),
  taskTable: document.querySelector("#task-table"),
  workOrders: document.querySelector("#work-order-list"),
  timeline: document.querySelector("#timeline"),
  scenarioControl: document.querySelector("#scenario-control"),
  controlDescription: document.querySelector("#control-description"),
  startButton: document.querySelector("#start-button"),
  stopButton: document.querySelector("#stop-button"),
  controlState: document.querySelector("#control-state-chip"),
  controlMessage: document.querySelector("#control-message"),
  controlLog: document.querySelector("#control-log-tail"),
};

let selectedRunId = null;
let latestRunId = null;
let refreshCounter = 0;
let followLatestRun = false;
let controlScenarios = [];
let roadMap = null;
let mapProject = null;

const labels = {
  completed: "已完成",
  executing: "执行中",
  assigned: "已派发",
  pending: "待处理",
  timed_out: "已超时",
  cancelled: "已取消",
  closed: "已关闭",
  in_progress: "执行中",
  pending_review: "待复核",
  escalated: "已升级",
  routine_inspection: "常规巡检",
  risk_review: "风险复核",
  road_control: "道路管控",
  emergency_response: "应急响应",
  blue: "蓝色",
  yellow: "黄色",
  orange: "橙色",
  red: "红色",
};

function escapeHtml(value) {
  return String(value ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function label(value) {
  return labels[value] || value || "—";
}

async function loadRuns() {
  const response = await fetch("/api/runs", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取运行列表");
  const data = await response.json();
  const runs = data.runs || [];
  latestRunId = runs.length ? runs[0].run_id : null;

  if (followLatestRun && latestRunId) selectedRunId = latestRunId;
  if (!selectedRunId && latestRunId) selectedRunId = latestRunId;
  if (!runs.some((item) => item.run_id === selectedRunId)) {
    selectedRunId = latestRunId;
  }

  const previous = ui.runSelect.value;
  ui.runSelect.innerHTML = runs
    .map((run) => {
      const suffix = run.completed ? run.status : "RUNNING";
      return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id)} · ${escapeHtml(suffix)}</option>`;
    })
    .join("");
  ui.runSelect.value = selectedRunId || previous;
}

async function loadControlScenarios() {
  const response = await fetch("/api/control/scenarios", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取控制场景");
  const data = await response.json();
  controlScenarios = data.scenarios || [];
  ui.scenarioControl.innerHTML = controlScenarios.map((scenario) => `
    <option value="${escapeHtml(scenario.scenario_id)}">${escapeHtml(scenario.display_name)}</option>
  `).join("");
  updateControlDescription();
}

function updateControlDescription() {
  const selected = controlScenarios.find(
    (item) => item.scenario_id === ui.scenarioControl.value
  );
  ui.controlDescription.textContent = selected
    ? `${selected.description} · 最多 ${selected.ticks} tick`
    : "暂无可启动场景";
}

async function loadControlStatus() {
  const response = await fetch("/api/control/status", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取控制状态");
  renderControlStatus(await response.json());
}

function renderControlStatus(status) {
  const stateLabels = {
    idle: "空闲",
    running: "运行中",
    stopping: "停止中",
    finished: "已结束",
  };
  ui.controlState.textContent = stateLabels[status.state] || status.state;
  ui.controlState.classList.toggle("error", status.exit_code !== null && status.exit_code !== 0);
  ui.startButton.disabled = Boolean(status.running);
  ui.stopButton.disabled = !status.running || status.state === "stopping";
  ui.scenarioControl.disabled = Boolean(status.running);
  ui.controlMessage.textContent = status.running
    ? `${status.scenario_id} · PID ${status.pid}`
    : status.scenario_id
      ? `${status.scenario_id} · 退出码 ${status.exit_code ?? "—"}`
      : "CARLA 需已在本机运行";
  ui.controlLog.textContent = (status.log_tail || []).join("\n") || "尚无控制日志";
  if (status.state === "finished") followLatestRun = false;
}

async function postControl(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "控制请求失败");
  return data;
}

async function startScenario() {
  const selected = controlScenarios.find(
    (item) => item.scenario_id === ui.scenarioControl.value
  );
  if (!selected) return;
  const confirmed = window.confirm(
    `将启动“${selected.display_name}”。\n\n程序会重新加载 Town03，当前 CARLA 世界中的车辆和状态将被清除。是否继续？`
  );
  if (!confirmed) return;
  ui.startButton.disabled = true;
  const status = await postControl("/api/control/start", {
    scenario_id: selected.scenario_id,
  });
  followLatestRun = true;
  renderControlStatus(status);
}

async function stopScenario() {
  const confirmed = window.confirm(
    "将请求安全中断当前 Demo，并释放车辆控制器。是否继续？"
  );
  if (!confirmed) return;
  ui.stopButton.disabled = true;
  renderControlStatus(await postControl("/api/control/stop"));
}

async function loadState() {
  if (!selectedRunId) {
    showConnection("暂无运行", true);
    return;
  }
  const query = new URLSearchParams({ run_id: selectedRunId });
  const response = await fetch(`/api/state?${query}`, { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取运行状态");
  renderState(await response.json());
  showConnection("数据已连接", false);
}

async function loadRoadMap() {
  const response = await fetch("/api/map", { cache: "no-store" });
  if (!response.ok) throw new Error("Town03路网暂不可用");
  roadMap = await response.json();
  mapProject = createMapProjection(roadMap.bounds);
  renderRoadLayer();
}

function renderState(state) {
  const metrics = state.metrics || {};
  const risk = state.current_risk;
  ui.scenario.textContent = state.scenario_id || "运行场景";
  ui.runId.textContent = state.run_id;
  ui.status.textContent = state.status;
  ui.status.style.color = state.status === "PASS" ? "var(--accent)" :
    state.status === "FAIL" ? "var(--danger)" : "var(--warn)";
  ui.tick.textContent = state.latest_tick ?? 0;
  ui.tasks.textContent = `${metrics.completed_task_count || 0} / ${metrics.task_count || 0}`;
  ui.taskRate.textContent = `${Math.round((metrics.task_completion_rate || 0) * 100)}% 完成率`;
  ui.orders.textContent = `${metrics.closed_work_order_count || 0} / ${metrics.work_order_count || 0}`;
  ui.restrictions.textContent = metrics.active_restriction_count || 0;
  ui.risk.textContent = risk ? label(risk.level) : "—";
  ui.riskTrend.textContent = risk ? `${label(risk.trend)} · ${risk.zone_id}` : "尚无评估";

  renderFleet(
    state.vehicles || [],
    state.zones || [],
    state.vehicle_trails || {},
    state.road_restrictions || []
  );
  renderRisk(risk, state.risk_assessments || []);
  renderTasks(state.tasks || []);
  renderOrders(state.work_orders || []);
  renderTimeline(state.timeline || []);

  const boundary = state.capability_boundary || {};
  ui.boundary.textContent = boundary.route_avoidance_enforced
    ? "已接入路段级绕行；仍需按目标矿区验证道路与装备参数。"
    : "Town03普通车辆代理；风险数据为合成数据，道路限制尚未接入路网边级绕行。";
}

function createMapProjection(bounds) {
  if (!bounds) return null;
  const width = 1000;
  const height = 560;
  const padding = 24;
  const spanX = Math.max(Number(bounds.max_x) - Number(bounds.min_x), 1);
  const spanY = Math.max(Number(bounds.max_y) - Number(bounds.min_y), 1);
  const scale = Math.min(
    (width - padding * 2) / spanX,
    (height - padding * 2) / spanY
  );
  const drawnWidth = spanX * scale;
  const drawnHeight = spanY * scale;
  const offsetX = (width - drawnWidth) / 2;
  const offsetY = (height - drawnHeight) / 2;
  return (position) => ({
    x: offsetX + (Number(position.x) - Number(bounds.min_x)) * scale,
    y: height - offsetY - (Number(position.y) - Number(bounds.min_y)) * scale,
  });
}

function svgElement(tag, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  return element;
}

function renderRoadLayer() {
  ui.roadLayer.replaceChildren();
  if (!roadMap || !mapProject) return;
  (roadMap.polylines || []).forEach((line) => {
    const points = (line.points || []).map(([x, y]) => {
      const point = mapProject({ x, y });
      return `${point.x.toFixed(1)},${point.y.toFixed(1)}`;
    }).join(" ");
    if (!points) return;
    ui.roadLayer.appendChild(svgElement("polyline", {
      points,
      class: "road-line",
    }));
  });
}

function renderFleet(vehicles, zones, trails, restrictions) {
  ui.fleetMap.querySelectorAll(".empty").forEach((item) => item.remove());
  ui.trailLayer.replaceChildren();
  ui.zoneLayer.replaceChildren();
  ui.vehicleLayer.replaceChildren();
  if (!vehicles.length) {
    ui.fleetMap.insertAdjacentHTML("beforeend", '<p class="empty">等待车辆位置</p>');
    ui.vehicleList.innerHTML = "";
    return;
  }

  if (!mapProject) {
    const allPositions = [
      ...vehicles.map((item) => item.position),
      ...zones.map((item) => item.position),
    ].filter(Boolean);
    const xs = allPositions.map((point) => Number(point.x) || 0);
    const ys = allPositions.map((point) => Number(point.y) || 0);
    mapProject = createMapProjection({
      min_x: Math.min(...xs) - 10,
      max_x: Math.max(...xs) + 10,
      min_y: Math.min(...ys) - 10,
      max_y: Math.max(...ys) + 10,
    });
  }

  Object.entries(trails).forEach(([vehicleId, values]) => {
    const points = values.map((position) => {
      const point = mapProject(position);
      return `${point.x.toFixed(1)},${point.y.toFixed(1)}`;
    }).join(" ");
    if (points) {
      ui.trailLayer.appendChild(svgElement("polyline", {
        points,
        class: "vehicle-trail",
        "data-vehicle-id": vehicleId,
      }));
    }
  });

  const restrictedZoneIds = new Set(
    restrictions
      .filter((item) => item.status === "active")
      .map((item) => item.zone_id)
  );
  zones.forEach((zone) => {
    if (!zone.position) return;
    const point = mapProject(zone.position);
    ui.zoneLayer.appendChild(svgElement("circle", {
      cx: point.x,
      cy: point.y,
      r: restrictedZoneIds.has(zone.zone_id) ? 8 : 5,
      class: `zone-point ${restrictedZoneIds.has(zone.zone_id) ? "restricted" : ""}`,
    }));
    const text = svgElement("text", {
      x: point.x + 9,
      y: point.y - 7,
      class: "map-label",
    });
    text.textContent = zone.display_name || zone.zone_id;
    ui.zoneLayer.appendChild(text);
  });

  vehicles.forEach((vehicle) => {
    const position = vehicle.position || { x: 0, y: 0 };
    const point = mapProject(position);
    ui.vehicleLayer.appendChild(svgElement("circle", {
      cx: point.x,
      cy: point.y,
      r: 7,
      class: `map-vehicle ${vehicle.health === "fault" ? "fault" : ""}`,
    }));
    const text = svgElement("text", {
      x: point.x + 10,
      y: point.y + 16,
      class: "map-label",
    });
    text.textContent = vehicle.vehicle_id;
    ui.vehicleLayer.appendChild(text);
  });

  ui.vehicleList.innerHTML = vehicles.map((vehicle) => `
    <article class="vehicle-card">
      <strong>${escapeHtml(vehicle.display_name || vehicle.vehicle_id)}</strong>
      <span>${escapeHtml(label(vehicle.task_status))} · ${Number(vehicle.speed_mps || 0).toFixed(1)} m/s</span>
      <span>${escapeHtml(vehicle.current_task_id || "当前无任务")}</span>
    </article>
  `).join("");
}

function renderRisk(risk, assessments) {
  document.querySelectorAll(".risk-scale div").forEach((item) => {
    item.classList.toggle("active", Boolean(risk && item.dataset.level === risk.level));
  });
  if (!risk) {
    ui.riskDetail.innerHTML = '<p class="empty">等待风险评估</p>';
    renderRiskTrend([]);
    return;
  }
  renderRiskTrend(assessments);
  const reasons = (risk.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const guidance = risk.guidance || {};
  const impacts = (guidance.impacts || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const prevention = (guidance.prevention_measures || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const environment = (guidance.environment_considerations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const actions = (guidance.planned_actions || []).map((item) =>
    `<li>${escapeHtml(label(item.task_type))} → ${escapeHtml(item.zone_id)}</li>`
  ).join("");
  ui.riskDetail.innerHTML = `
    <strong>${escapeHtml(label(risk.level))}风险</strong>
    <p class="muted">样本 ${escapeHtml(risk.sample_id)} · Tick ${escapeHtml(risk.tick)}</p>
    <p><strong>评估依据</strong></p>
    <ul>${reasons}</ul>
    ${impacts ? `<p><strong>可能影响</strong></p><ul>${impacts}</ul>` : ""}
    ${prevention ? `<p><strong>预防措施</strong></p><ul>${prevention}</ul>` : ""}
    ${environment ? `<p><strong>环境注意项</strong></p><ul>${environment}</ul>` : ""}
    ${actions ? `<p><strong>规划动作</strong></p><ul>${actions}</ul>` : ""}
  `;
}

function renderRiskTrend(assessments) {
  ui.riskTrend.replaceChildren();
  if (!assessments.length) return;
  const levels = { blue: 0, yellow: 1, orange: 2, red: 3 };
  const colors = {
    blue: "#4f91ff",
    yellow: "#f3d452",
    orange: "#ffb23e",
    red: "#ff665f",
  };
  const maxTick = Math.max(...assessments.map((item) => Number(item.tick) || 0), 1);
  [0, 1, 2, 3].forEach((rank) => {
    const y = 120 - rank * 31;
    ui.riskTrend.appendChild(svgElement("line", {
      x1: 24, y1: y, x2: 500, y2: y, class: "risk-axis",
    }));
  });
  const points = assessments.map((item) => ({
    x: 28 + ((Number(item.tick) || 0) / maxTick) * 462,
    y: 120 - (levels[item.level] || 0) * 31,
    item,
  }));
  ui.riskTrend.appendChild(svgElement("polyline", {
    points: points.map((point) => `${point.x},${point.y}`).join(" "),
    class: "risk-path",
  }));
  points.forEach((point) => {
    ui.riskTrend.appendChild(svgElement("circle", {
      cx: point.x,
      cy: point.y,
      r: 6,
      fill: colors[point.item.level] || "#fff",
      class: "risk-point",
    }));
    const text = svgElement("text", {
      x: point.x,
      y: 142,
      class: "risk-tick-label",
    });
    text.textContent = `T${point.item.tick}`;
    ui.riskTrend.appendChild(text);
  });
}

function renderTasks(tasks) {
  ui.taskTable.innerHTML = tasks.length ? tasks.map((task) => `
    <tr>
      <td title="${escapeHtml(task.task_id)}">${escapeHtml(task.task_id)}</td>
      <td>${escapeHtml(label(task.task_type))}</td>
      <td>${escapeHtml(task.zone_id)}</td>
      <td>${escapeHtml(task.assigned_vehicle_id)}</td>
      <td><span class="status-pill ${escapeHtml(task.status)}">${escapeHtml(label(task.status))}</span></td>
    </tr>
  `).join("") : '<tr><td colspan="5">暂无任务</td></tr>';
}

function renderOrders(orders) {
  ui.workOrders.innerHTML = orders.length ? orders.map((order) => `
    <article class="stack-item">
      <div>
        <strong>${escapeHtml(order.order_type || order.work_order_id)}</strong>
        <span class="status-pill ${escapeHtml(order.status)}">${escapeHtml(label(order.status))}</span>
      </div>
      <p>${escapeHtml(order.assigned_vehicle_id || "尚未派发")} · ${escapeHtml(order.task_id)}</p>
    </article>
  `).join("") : '<p class="empty">暂无风险工单</p>';
}

function renderTimeline(timeline) {
  const recent = [...timeline].reverse().slice(0, 40);
  ui.timeline.innerHTML = recent.length ? recent.map((event) => {
    const detail = event.task_id || event.work_order_id || event.restriction_id ||
      event.vehicle_id || event.level || event.status || "";
    return `
      <article class="timeline-item">
        <strong>${escapeHtml(event.event_type)}</strong>
        <p>Tick ${escapeHtml(event.tick ?? "—")} · ${escapeHtml(detail)}</p>
      </article>
    `;
  }).join("") : '<p class="empty">暂无事件</p>';
}

function showConnection(text, isError) {
  ui.connection.textContent = text;
  ui.connection.classList.toggle("error", isError);
}

ui.runSelect.addEventListener("change", () => {
  followLatestRun = false;
  selectedRunId = ui.runSelect.value;
  loadState().catch((error) => showConnection(error.message, true));
});
ui.scenarioControl.addEventListener("change", updateControlDescription);
ui.startButton.addEventListener("click", () => {
  startScenario().catch((error) => showConnection(error.message, true));
});
ui.stopButton.addEventListener("click", () => {
  stopScenario().catch((error) => showConnection(error.message, true));
});

async function refresh() {
  try {
    refreshCounter += 1;
    if (!roadMap && refreshCounter % 5 === 1) {
      loadRoadMap().catch(() => {});
    }
    if (!controlScenarios.length) await loadControlScenarios();
    if (refreshCounter % 5 === 1) await loadRuns();
    await loadControlStatus();
    await loadState();
  } catch (error) {
    showConnection(error.message, true);
  }
}

refresh();
setInterval(refresh, 1000);
