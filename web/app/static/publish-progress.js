"use strict";

(() => {
  const stageLabels = {
    QUEUED: "等待执行",
    PREPARING: "准备发布",
    READING_REQUIREMENTS: "读取依赖",
    RESOLVING_DEPENDENCIES: "解析依赖",
    CHECKING_RUNTIME_CACHE: "检查 Runtime 缓存",
    REUSING_RUNTIME: "复用 Runtime",
    RESOLVING_RUNTIME: "准备 Runtime",
    WAITING_RUNTIME: "等待 Runtime",
    BUILDING_RUNTIME: "构建 Runtime",
    VERIFYING_RUNTIME: "验证 Runtime",
    RUNTIME_READY: "Runtime 已就绪",
    RUNTIME_FAILED: "Runtime 构建失败",
    BUILDING_PACKAGE: "生成发布包",
    WAITING_LOCK: "等待边端发布锁",
    CHECKING_BUNDLE_REUSE: "检查 Bundle 复用",
    ASSEMBLING_PROCESSORS: "整理 Processor",
    DOWNLOADING_DEPENDENCIES: "下载依赖",
    BUILDING_BUNDLE: "生成 Edge Bundle",
    PACKAGING_BUNDLE: "打包 Edge Bundle",
    VERIFYING_ARTIFACT: "校验发布产物",
    PERSISTING: "保存发布记录",
    COMPLETED: "发布完成",
    FAILED: "发布失败",
  };

  const workerHealthyMs = 25000;
  const workerStaleMs = 60000;

  let activeJobId = null;
  let lastStage = null;
  let lastSeq = 0;
  let rows = new Map();
  let terminal = false;
  let connectionMode = "SSE";
  let lastWorkerSignalAt = 0;
  let stageStartedAt = 0;
  let watchdogTimer = null;

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  function formatDuration(ms) {
    const seconds = Math.max(0, Math.floor(ms / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.floor(seconds / 60);
    const remain = seconds % 60;
    if (minutes < 60) return `${minutes} 分 ${remain} 秒`;
    const hours = Math.floor(minutes / 60);
    return `${hours} 小时 ${minutes % 60} 分`;
  }

  function ensurePanel() {
    let panel = document.getElementById("publishLiveProgress");
    if (panel) return panel;

    const publishPanel = document.getElementById("publishPanel");
    if (!publishPanel) throw new Error("publishPanel not found");

    panel = node("div", "publish-live-progress hidden");
    panel.id = "publishLiveProgress";
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");

    const header = node("div", "publish-progress-header");
    const heading = node("div");
    heading.append(node("strong", "", "实时发布进度"));
    heading.append(node(
      "div",
      "muted",
      "任务在服务端后台执行；关闭页面不会主动取消发布。"
    ));
    header.append(heading);

    const badge = node("span", "label-pill", "等待中");
    badge.id = "publishProgressBadge";
    header.append(badge);
    panel.append(header);

    const message = node("div", "publish-progress-message", "等待任务开始…");
    message.id = "publishProgressMessage";
    panel.append(message);

    const health = node("div", "publish-progress-health");
    health.id = "publishProgressHealth";
    panel.append(health);

    const bar = node("div", "publish-progress-bar");
    const fill = node("span");
    fill.id = "publishProgressBarFill";
    bar.append(fill);
    panel.append(bar);

    const list = node("ol", "publish-progress-list");
    list.id = "publishProgressList";
    panel.append(list);

    const terminalBox = node("div", "publish-progress-terminal hidden");
    terminalBox.id = "publishProgressTerminal";
    terminalBox.setAttribute("role", "alert");
    panel.append(terminalBox);

    const summary = document.getElementById("publishSummary");
    publishPanel.insertBefore(panel, summary);
    return panel;
  }

  function setMarker(row, state) {
    const marker = row._marker;
    marker.replaceChildren();
    row.classList.remove("is-active", "is-done", "is-interrupted");

    if (state === "done") {
      row.classList.add("is-done");
      marker.textContent = "✓";
      return;
    }

    if (state === "active") {
      row.classList.add("is-active");
      for (let i = 0; i < 3; i++) {
        marker.append(node("span", "publish-progress-running-dot"));
      }
      return;
    }

    row.classList.add("is-interrupted");
    marker.textContent = "•••";
  }

  function markCurrentDone() {
    if (!lastStage) return;
    const current = rows.get(lastStage);
    if (current) setMarker(current, "done");
  }

  function interruptCurrent() {
    if (!lastStage) return;
    const current = rows.get(lastStage);
    if (current) setMarker(current, "interrupted");
  }

  function markPreviousDone(nextStage) {
    if (!lastStage || lastStage === nextStage) return;
    const previous = rows.get(lastStage);
    if (previous) setMarker(previous, "done");
  }

  function touchWorkerSignal(event) {
    const seq = Number(event?.seq || 0);
    if (seq && seq <= lastSeq) return false;
    if (seq) lastSeq = seq;
    lastWorkerSignalAt = performance.now();
    return true;
  }

  function setConnectionMode(mode) {
    connectionMode = mode;
    updateHealthText();
  }

  function updateHealthText() {
    const health = document.getElementById("publishProgressHealth");
    if (!health) return;

    health.className = "publish-progress-health";
    if (terminal) {
      health.textContent = "";
      return;
    }

    if (!lastWorkerSignalAt) {
      health.textContent = connectionMode === "POLL"
        ? "正在通过状态查询等待后台任务…"
        : "正在等待后台任务心跳…";
      return;
    }

    const now = performance.now();
    const age = now - lastWorkerSignalAt;
    const elapsed = stageStartedAt ? now - stageStartedAt : 0;
    const modeText = connectionMode === "POLL" ? "状态轮询" : "实时连接";

    if (age < workerHealthyMs) {
      health.classList.add("is-healthy");
      health.textContent =
        `后台任务心跳正常 · ${modeText}` +
        (elapsed ? ` · 当前步骤已运行 ${formatDuration(elapsed)}` : "");
      return;
    }

    if (age < workerStaleMs) {
      health.classList.add("is-warning");
      health.textContent =
        `已 ${formatDuration(age)} 未收到新的后台任务心跳；` +
        "可能只是当前步骤耗时较长，页面会继续等待。";
      return;
    }

    health.classList.add("is-stale");
    health.textContent =
      `已 ${formatDuration(age)} 未收到后台任务心跳。` +
      "发布 Worker 或其依赖调用可能异常；请先查询任务状态，不要立即重复发布。";
  }

  function startWatchdog() {
    stopWatchdog();
    watchdogTimer = window.setInterval(updateHealthText, 1000);
    updateHealthText();
  }

  function stopWatchdog() {
    if (watchdogTimer !== null) {
      window.clearInterval(watchdogTimer);
      watchdogTimer = null;
    }
  }

  function reset(job) {
    activeJobId = String(job?.jobId || "");
    lastStage = null;
    lastSeq = 0;
    rows = new Map();
    terminal = false;
    connectionMode = "SSE";
    lastWorkerSignalAt = 0;
    stageStartedAt = 0;

    const panel = ensurePanel();
    panel.className = "publish-live-progress";
    document.getElementById("publishProgressList").replaceChildren();

    const terminalBox = document.getElementById("publishProgressTerminal");
    terminalBox.replaceChildren();
    terminalBox.className = "publish-progress-terminal hidden";

    document.getElementById("publishProgressMessage").textContent =
      "发布任务已创建，等待后台开始执行…";

    const badge = document.getElementById("publishProgressBadge");
    badge.textContent = job?.status || "PENDING";
    badge.className = "label-pill";

    document.getElementById("publishProgressBarFill").style.width = "0%";
    startWatchdog();
  }

  function render(event) {
    if (!event || typeof event !== "object" || terminal) return;

    const panel = ensurePanel();
    panel.classList.remove("hidden");

    const stage = String(event.stage || "RUNNING");
    const isNewStage = stage !== lastStage;
    if (isNewStage) {
      markPreviousDone(stage);
      lastStage = stage;
      stageStartedAt = performance.now();
    }

    let row = rows.get(stage);
    if (!row) {
      row = node("li", "publish-progress-item");
      const marker = node("span", "publish-progress-marker");
      marker.setAttribute("aria-hidden", "true");
      const body = node("div");
      const title = node("strong", "publish-progress-title", stageLabels[stage] || stage);
      const detail = node("div", "publish-progress-detail", event.message || "");
      body.append(title, detail);
      row.append(marker, body);
      row._marker = marker;
      row._detail = detail;
      document.getElementById("publishProgressList").append(row);
      rows.set(stage, row);
    }

    setMarker(row, "active");
    row._detail.textContent = event.message || "";

    const newSignal = touchWorkerSignal(event);
    if (newSignal || isNewStage) updateHealthText();

    document.getElementById("publishProgressMessage").textContent =
      event.message || stageLabels[stage] || stage;

    const badge = document.getElementById("publishProgressBadge");
    badge.textContent = event.status || "RUNNING";
    badge.className = "label-pill";

    const percent = Number(event.progress?.percent);
    if (Number.isFinite(percent)) {
      document.getElementById("publishProgressBarFill").style.width =
        `${Math.max(0, Math.min(100, percent))}%`;
    }
  }

  function parseFailure(error) {
    let detail = error;
    if (typeof error === "string") {
      const value = error.trim();
      if (value) {
        try {
          const parsed = JSON.parse(value);
          if (parsed && typeof parsed === "object") detail = parsed;
        } catch {
          detail = value;
        }
      }
    }

    if (detail && typeof detail === "object") {
      return {
        code: typeof detail.code === "string" ? detail.code : "",
        message: typeof detail.message === "string"
          ? detail.message
          : "发布任务执行失败",
        resolverError: typeof detail.resolverError === "string"
          ? detail.resolverError
          : "",
        raw: detail,
      };
    }

    return {
      code: "",
      message: typeof detail === "string" && detail
        ? detail
        : "发布任务执行失败",
      resolverError: "",
      raw: detail,
    };
  }

  function renderFailure(job) {
    const terminalBox = document.getElementById("publishProgressTerminal");
    terminalBox.replaceChildren();
    terminalBox.className = "publish-progress-terminal is-failed";

    const icon = node("div", "publish-progress-terminal-icon", "×");
    icon.setAttribute("aria-hidden", "true");

    const body = node("div", "publish-progress-terminal-body");
    body.append(node("strong", "publish-progress-terminal-title", "发布失败"));

    const failure = parseFailure(job?.error);
    if (failure.code) {
      body.append(node("code", "publish-progress-error-code", failure.code));
    }
    body.append(node("div", "publish-progress-error-message", failure.message));

    if (failure.resolverError) {
      body.append(node("pre", "publish-progress-error-primary", failure.resolverError));
    }

    if (failure.raw !== undefined && failure.raw !== null) {
      const details = node("details", "publish-progress-error-details");
      details.append(node("summary", "", "查看完整错误详情"));
      details.append(node(
        "pre",
        "",
        typeof failure.raw === "string"
          ? failure.raw
          : JSON.stringify(failure.raw, null, 2),
      ));
      body.append(details);
    }

    terminalBox.append(icon, body);
  }

  function finish(job) {
    const ready = job?.status === "READY";
    const failed = job?.status === "FAILED";
    terminal = ready || failed;
    stopWatchdog();

    const panel = ensurePanel();
    const badge = document.getElementById("publishProgressBadge");

    if (ready) {
      markCurrentDone();
      panel.classList.add("is-ready");
      badge.textContent = "READY";
      badge.className = "success-pill";
      document.getElementById("publishProgressMessage").textContent = "发布完成";
      document.getElementById("publishProgressBarFill").style.width = "100%";
      document.getElementById("publishProgressHealth").textContent =
        "后台任务已正常完成。";
      document.getElementById("publishProgressHealth").className =
        "publish-progress-health is-healthy";
      return;
    }

    if (failed) {
      interruptCurrent();
      panel.classList.add("is-failed");
      badge.textContent = "FAILED";
      badge.className = "error-pill";
      document.getElementById("publishProgressMessage").textContent = "发布失败";
      document.getElementById("publishProgressHealth").textContent =
        "任务已结束，失败原因见下方。";
      document.getElementById("publishProgressHealth").className =
        "publish-progress-health is-failed";
      renderFailure(job);
      return;
    }

    document.getElementById("publishProgressMessage").textContent = "发布任务已结束";
  }

  function parseBlock(block) {
    let event = "message";
    let id = null;
    const data = [];

    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("id:")) id = line.slice(3).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }

    if (!data.length) return null;
    return {event, id, data: JSON.parse(data.join("\n"))};
  }

  async function stream(jobId) {
    setConnectionMode("SSE");
    const response = await fetch(
      `/api/publish-jobs/${encodeURIComponent(jobId)}/events`,
      {headers: {Accept: "text/event-stream"}},
    );
    if (!response.ok || !response.body) {
      throw new Error(`SSE HTTP ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});

      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";

      for (const block of blocks) {
        if (!block.trim() || block.trimStart().startsWith(":")) continue;
        const parsed = parseBlock(block);
        if (!parsed) continue;

        if (parsed.event === "progress") {
          render(parsed.data);
        } else if (parsed.event === "completed") {
          finish(parsed.data);
          try { await reader.cancel(); } catch {}
          return parsed.data;
        } else if (parsed.event === "failed") {
          finish(parsed.data);
          try { await reader.cancel(); } catch {}
          return parsed.data;
        }
      }

      if (done) break;
    }

    throw new Error("SSE ended before a terminal publish status");
  }

  async function status(jobId) {
    const response = await fetch(
      `/api/publish-jobs/${encodeURIComponent(jobId)}`,
      {headers: {Accept: "application/json"}},
    );
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; }
    catch { data = raw; }

    if (!response.ok) {
      throw new Error(
        `GET publish job HTTP ${response.status}: ${
          typeof data === "string" ? data : JSON.stringify(data)
        }`
      );
    }
    return data;
  }

  async function poll(jobId) {
    setConnectionMode("POLL");
    while (true) {
      const job = await status(jobId);
      if (job?.progress) render(job.progress);
      if (job?.status === "READY" || job?.status === "FAILED") {
        finish(job);
        return job;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  async function monitor(jobId) {
    if (!jobId) throw new Error("publish jobId is required");
    try {
      return await stream(jobId);
    } catch (error) {
      console.warn(
        "Publish SSE unavailable; falling back to GET Status polling",
        error,
      );
      return await poll(jobId);
    }
  }

  globalThis.PublishProgress = {
    reset,
    render,
    finish,
    monitor,
    status,
    setConnectionMode,
    get activeJobId() { return activeJobId; },
  };
})();
