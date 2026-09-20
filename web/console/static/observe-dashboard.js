"use strict";
/* Independent read-only console. No dependency on app/static/ui.js. */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat("zh-CN");
  const labels = {
    publish: {title:"Publish Service",hint:"算子、契约、后端变体和发布任务",initial:"P"},
    build: {title:"Build Service",hint:"运行环境与构建任务",initial:"B"},
    runner: {title:"Runner Engine",hint:"Release、执行与任务记录",initial:"R"},
  };
  const state = {
    active: "database", snapshot: null,
    pending: false, timer: null, lastSuccess: null,
  };
  const div = (tag, cls="", value) => {
    const node = document.createElement(tag);
    if (cls) node.className=cls;
    if (value !== undefined && value !== null) node.textContent=String(value);
    return node;
  };
  const number = (value) => Number.isFinite(value) ? fmt.format(value) : "—";
  const isNum = (value) => typeof value==="number" && Number.isFinite(value);
  const percent = (value) => isNum(value) ? `${value.toFixed(1)}%` : "—";
  const when = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" :
      new Intl.DateTimeFormat("zh-CN",{
        year:"numeric",month:"2-digit",day:"2-digit",
        hour:"2-digit",minute:"2-digit",second:"2-digit",
      }).format(date);
  };
  function duration(seconds) {
    if (!isNum(seconds) || seconds < 0) return "—";
    seconds=Math.floor(seconds);
    const days=Math.floor(seconds/86400);
    const hours=Math.floor(seconds%86400/3600);
    const minutes=Math.floor(seconds%3600/60);
    if(days) return `${days}天 ${hours}小时`;
    if(hours) return `${hours}小时 ${minutes}分`;
    if(minutes) return `${minutes}分 ${seconds%60}秒`;
    return `${seconds}秒`;
  }
  function statusBadge(value) {
    const names={
      ok:"已连接",unconfigured:"未配置",error:"连接异常",
    };
    return div("span",`health-badge ${value||"unconfigured"}`,names[value]||"未知");
  }
  function makeTextCard(id,value) {$(id).textContent=value;}
  function metricValue(report,key) {
    if (!report || report.status!=="ok") return null;
    const result=(report.metrics||[]).find(item=>item.key===key);
    return result && isNum(result.count)?result.count:null;
  }
  function overviewStatus(db, sb) {
    const dbRecords=Object.values(db||{});
    const statuses=dbRecords.map(item=>item.status).concat(sb?.status||[]);
    const good=statuses.filter(item=>item==="ok").length;
    const configured=statuses.filter(item=>item!=="unconfigured").length;
    let name=good===4?"数据已更新":
      good>0?"部分数据可用":configured===0?"等待配置":"暂不可用";
    let cls=good===4?"ok":good>0?"partial":configured===0?"":"error";
    $("globalHealth").className=`connection-pill ${cls}`;
    $("globalHealth").replaceChildren(div("span","health-dot"),document.createTextNode(name));
  }
  function barGroup(title, data, total) {
    if (!Array.isArray(data)||!data.length) return null;
    const wrapper=div("div","group-block");
    wrapper.append(div("p","group-title",title));
    for(const item of data){
      const row=div("div","group-row");
      const label=div("span","group-label",item.label);
      label.title=item.label;
      const track=div("div","group-track");
      const fill=div("div","group-fill");
      fill.style.width=`${Math.min(100,Math.max(0,total>0?item.count*100/total:0))}%`;
      track.append(fill);
      row.append(label,track,div("strong","group-count",number(item.count)));
      wrapper.append(row);
    }
    return wrapper;
  }
  function metricCard(metric) {
    const card=div("article","metric-card");
    const head=div("div","metric-top");
    head.append(div("div","metric-name",metric.label));
    const mapping={
      exact:"已连接",missing:"不可用",
      ambiguous:"需确认",error:"查询失败",unconfigured:"未配置",
    };
    head.append(div("span",`quality-tag ${metric.quality}`,
      mapping[metric.quality]||"未知"));
    card.append(head,div("div","metric-count",number(metric.count)));
    const small=metric.recent24h===null || metric.recent24h===undefined
      ?"24 小时变化：暂无可靠时间字段"
      :`最近 24 小时新记录：${number(metric.recent24h)}`;
    card.append(div("div","metric-secondary",small));
    if(metric.table){
      const line=div("div","metric-secondary",`Table: ${metric.table}`);
      card.append(line);
    }
    if(metric.byStatus?.length){
      const group=barGroup(metric.key==="aliases"?"RESOLUTION KIND":
        "STATUS DISTRIBUTION",metric.byStatus,metric.count);
      if(group)card.append(group);
    }
    if(metric.byBackend?.length){
      const group=barGroup("BACKEND SPLIT",metric.byBackend,metric.count);
      if(group)card.append(group);
    }
    if(metric.note)card.append(div("p","metric-note",metric.note));
    return card;
  }
  function serviceCard(key,data) {
    const info=labels[key],card=div("article","service-card");
    const head=div("div","service-head");
    const left=div("div","service-head-left");
    left.append(div("span","service-icon",info.initial));
    const name=div("div");
    name.append(div("h3","",info.title),div("p","service-subtitle",info.hint));
    left.append(name);
    head.append(left,statusBadge(data?.status));
    card.append(head);
    const body=div("div","service-body");
    if(!data||data.status!=="ok"){
      body.append(div("p","service-warning",data?.message||"未配置该数据库"));
    } else {
      const recognized=(data.metrics||[]).filter(m=>isNum(m.count)).length;
      body.append(div("p","service-subtitle",
        `识别指标：${recognized}/${(data.metrics||[]).length} · 数据库类型：${data.dialect||"—"}`));
      const list=div("div","metric-list");
      for(const metric of data.metrics||[])list.append(metricCard(metric));
      body.append(list);
      if(Array.isArray(data.checks)&&data.checks.length){
        const section=div("div","exact-checks");
        section.append(div("h4","","运行状态与清理检查"));
        const grid=div("div","check-grid");
        for(const check of data.checks){
          const item=div("div","check-card "+(check.severity||"info"));
          item.append(div("span","check-name",check.label));
          item.append(div("strong","check-count",
            typeof check.count==="number"?number(check.count):"—"));
          grid.append(item);
        }
        section.append(grid);body.append(section);
      }
      if(data.tables?.length){
        const detail=div("details","table-inventory");
        detail.append(div("summary","","查看数据库结构（表名及字段）"));
        const inventory=div("div","inventory-grid");
        for(const table of data.tables){
          const box=div("div","inventory-item");
          box.append(div("code","",table.name));
          box.append(div("small","",table.columns?.join(" · ")||"无可展示字段"));
          inventory.append(box);
        }
        detail.append(inventory);
        body.append(detail);
      }else {
        body.append(div("p","service-warning","当前连接中没有发现可识别的业务表。"));
      }
    }
    card.append(body);
    return card;
  }
  function renderDatabase(sources) {
    const entries=Object.entries(labels);
    const connected=entries.filter(([k])=>sources?.[k]?.status==="ok").length;
    makeTextCard("connectedDb",`${connected}/3`);
    const p=metricValue(sources?.publish,"jobs");
    const b=metricValue(sources?.build,"environments");
    const r=metricValue(sources?.runner,"releases");
    makeTextCard("publishJobs",number(p));
    makeTextCard("buildEnvs",number(b));
    makeTextCard("runnerReleases",number(r));
    const coverage=entries.reduce((sum,[k])=>sum+
      (sources?.[k]?.metrics||[]).filter(m=>isNum(m.count)).length,0);
    const total=entries.reduce((sum,[k])=>sum+
      (sources?.[k]?.metrics||[]).length,0);
    makeTextCard("databaseCoverage",`${coverage}/${total} 项指标可用`);
    makeTextCard("dbNavBadge",`${connected}/3`);
    const host=$("databaseCards");
    host.replaceChildren();
    for(const [key] of entries) host.append(serviceCard(key,sources?.[key]));
  }
  function sortSandboxes(rows) {
    const value=$("sandboxSort").value;
    const copy=rows.slice();
    const factor=value==="newest"?1:-1;
    const key=value==="cpu"?"cpuPct":
      value==="memory"?"memUsedMiB":"ageSeconds";
    const get=(row)=>key==="ageSeconds"?row.ageSeconds:row.resources?.[key];
    copy.sort((a,b)=>{
      const aa=get(a),bb=get(b);
      if(!isNum(aa)&&!isNum(bb))return a.id.localeCompare(b.id);
      if(!isNum(aa))return 1;
      if(!isNum(bb))return -1;
      return factor*(aa-bb);
    });
    return copy;
  }
  function lifetime(record) {
    const created=record.createdAt?new Date(record.createdAt).valueOf():NaN;
    if(Number.isNaN(created))return duration(record.ageSeconds);
    return duration(Math.max(0,(Date.now()-created)/1000));
  }
  function ttlText(record){
    if(!record.expiresAt)return "无自动到期";
    const remains=(new Date(record.expiresAt).valueOf()-Date.now())/1000;
    if(!Number.isFinite(remains))return "—";
    if(remains<=0)return "已过期 / 待回收";
    return duration(remains);
  }
  function sandboxRow(item) {
    const tr=div("tr");
    const colId=div("td");
    const id=div("span","sandbox-id mono",item.id);
    id.title=item.id;
    colId.append(id);
    if(item.image||item.snapshotId){
      const source=item.image||`Snapshot: ${item.snapshotId}`;
      const label=div("span","sandbox-image",source);
      label.title=source;colId.append(label);
    }
    const colState=div("td");
    colState.append(div("span","state-tag",item.state||"—"));
    const uptime=div("td","metric-mini",lifetime(item));
    uptime.dataset.sandboxTime=item.id;
    const expiry=div("td","metric-mini",ttlText(item));
    expiry.dataset.sandboxExpiry=item.id;
    if(item.ttlSeconds!==null&&item.ttlSeconds<0)expiry.classList.add("ttl-expired");
    else if(item.ttlSeconds!==null&&item.ttlSeconds<3600)expiry.classList.add("ttl-soon");
    const cpu=div("td","metric-mini",percent(item.resources?.cpuPct));
    const mem=div("td","metric-mini",
      isNum(item.resources?.memUsedMiB)
        ? `${item.resources.memUsedMiB.toFixed(1)} / ${
          isNum(item.resources?.memTotalMiB)?
          item.resources.memTotalMiB.toFixed(0)+" MiB":"—"}`
        : "—"
    );
    if(isNum(item.resources?.memUsedMiB)&&
       isNum(item.resources?.memTotalMiB)&&item.resources.memTotalMiB>0){
      const track=div("div","memory-track"),fill=div("div","memory-fill");
      fill.style.width=`${Math.max(0,Math.min(100,
        item.resources.memUsedMiB*100/item.resources.memTotalMiB))}%`;
      track.append(fill);mem.append(track);
    }
    const status=div("td");
    const captions={
      ok:"已采样",disabled:"未启用",unconfigured:"待配置",
      unavailable:"采样失败",not_sampled:"超过采样上限",pending:"采集中",
    };
    const text=div("span",item.resourceStatus==="ok"?"resource-ok":
      item.resourceStatus==="unavailable"?"resource-unavailable":"resource-disabled",
      captions[item.resourceStatus]||"未采集");
    if(item.resourceNote)text.title=item.resourceNote;
    status.append(text);
    tr.append(colId,colState,uptime,expiry,cpu,mem,status);
    return tr;
  }
  function filteredRows(rows){
    const search=$("sandboxSearch").value.trim().toLocaleLowerCase();
    return sortSandboxes(rows.filter(item=>!search||[
      item.id,item.image||"",item.snapshotId||"",
    ].join(" ").toLocaleLowerCase().includes(search)));
  }
  function renderSandboxRows(){
    const report=state.snapshot?.sandboxes;
    const host=$("sandboxRows");
    host.replaceChildren();
    if(!report||report.status!=="ok"){
      host.append(makeEmpty(report?.message||"尚未采集 Sandbox 数据"));
      makeTextCard("tableCount","—");return;
    }
    const all=report.sandboxes||[], selected=filteredRows(all);
    if(!selected.length){
      host.append(makeEmpty(all.length?"没有匹配的 Sandbox":"当前没有 Running Sandbox"));
    }else{
      for(const entry of selected)host.append(sandboxRow(entry));
    }
    makeTextCard("tableCount",`显示 ${selected.length} / 已读取 ${all.length} 个`);
  }
  function makeEmpty(message){
    const tr=div("tr"),cell=div("td","empty-cell",message);
    cell.colSpan=7;tr.append(cell);return tr;
  }
  function renderSandbox(report){
    const valid=report?.status==="ok";
    makeTextCard("sandboxRunning",valid?number(report.runningTotal):"—");
    makeTextCard("sandboxListed",valid?number(report.returned):"—");
    makeTextCard("sandboxMeasured",valid?number(report.resourceMeasured):"—");
    const sample=(report?.sandboxes||[]).filter(r=>isNum(r.resources?.memUsedMiB));
    const sum=sample.reduce((value,r)=>value+r.resources.memUsedMiB,0);
    makeTextCard("sandboxMemory",valid&&sample.length?`${sum.toFixed(1)} MiB`:"—");
    makeTextCard("sandboxNavBadge",valid?number(report.runningTotal):"—");
    makeTextCard("sandboxCoverage",
      valid?`${report.returned} 个已读取 · ${report.resourceMeasured} 个已采样`:"暂无数据");
    const warning=$("sandboxWarning");
    let note="";
    if(report?.status!=="ok"){
      note=report?.message||"尚未配置 OpenSandbox Lifecycle API";
    }else if(report.truncated){
      note=`当前只读取前 ${report.returned} 个 Running Sandbox。Running 总数来自 Lifecycle API 分页元数据，列表并不完整；可提高 OBS_SANDBOX_MAX_ITEMS。`;
    }
    if(valid && report.returned && !report.metricsEnabled){
      note+=(note?"\n":"")+"CPU 与内存尚未启用。需要设置 execd 资源采集开关以及获准访问的端点域名。";
    }else if(valid && report.returned && report.resourceMeasured<report.returned){
      const unsampled=(report.sandboxes||[]).filter(
        row=>row.resourceStatus==="not_sampled"
      ).length;
      const unavailable=(report.sandboxes||[]).filter(
        row=>row.resourceStatus==="unavailable"
      ).length;
      if(unsampled)note+=(note?"\n":"")+
        `${unsampled} 个实例超过本轮资源采样上限；可调整 OBS_EXECD_SAMPLE_LIMIT。`;
      if(unavailable)note+=(note?"\n":"")+
        `${unavailable} 个实例资源采样失败，请检查 execd 授权与端点代理。`;
    }
    warning.textContent=note;
    warning.classList.toggle("hidden",!note);
    renderSandboxRows();
  }
  function setView(view){
    state.active=view;
    const db=view==="database";
    $("databaseView").classList.toggle("hidden",!db);
    $("sandboxView").classList.toggle("hidden",db);
    $("navDatabase").classList.toggle("active",db);
    $("navSandbox").classList.toggle("active",!db);
    $("navDatabase").setAttribute("aria-current",db?"page":"false");
    $("navSandbox").setAttribute("aria-current",!db?"page":"false");
    $("pageTitle").textContent=db?"数据库指标":"OpenSandbox";
    $("pageSubtitle").textContent=db?
      "Publish、Build 与 Runner 的持久化状态汇总。":
      "查看正在运行的 Sandbox、存续时间与资源使用情况。";
    $("pageTitle").focus({preventScroll:true});
    window.scrollTo({top:0,behavior:"instant"});
  }
  async function refresh(){
    if(state.pending)return;
    state.pending=true;
    $("loadingStrip").classList.remove("hidden");
    $("refreshBtn").disabled=true;
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),60000);
    try{
      const response=await fetch("/observe/api/overview",{
        method:"GET",headers:{Accept:"application/json"},
        credentials:"same-origin",cache:"no-store",signal:controller.signal,
      });
      if(!response.ok)throw new Error(`HTTP ${response.status}`);
      const data=await response.json();
      if(!data||!data.databases||!data.sandboxes)throw new Error("响应格式不正确");
      state.snapshot=data;
      state.lastSuccess=Date.now();
      renderDatabase(data.databases);
      renderSandbox(data.sandboxes);
      overviewStatus(data.databases,data.sandboxes);
      makeTextCard("sampleTime",`采样时间：${when(data.sampledAt)}`);
      $("refreshDot").className="snapshot-dot ok";
      $("globalError").classList.add("hidden");
    }catch(error){
      const stale=state.lastSuccess?
        `\n页面仍保留上次采集数据（${when(new Date(state.lastSuccess).toISOString())}），可能已过期。`:"";
      $("globalError").textContent=`数据刷新失败：${error.name==="AbortError"?"请求超时":error.message}${stale}`;
      $("globalError").classList.remove("hidden");
      $("refreshDot").className="snapshot-dot";
      makeTextCard("sampleTime",stale?"上次成功采集（数据可能过期）":"采集失败");
      $("globalHealth").className="connection-pill error";
    }finally{
      clearTimeout(timeout);
      state.pending=false;
      $("loadingStrip").classList.add("hidden");
      $("refreshBtn").disabled=false;
    }
  }
  function updateTimer(){
    if(state.timer)clearInterval(state.timer);
    const ms=Number($("autoRefresh").value);
    if(ms>0)state.timer=setInterval(()=>{
      if(!document.hidden)refresh();
    },ms);
  }
  $("navDatabase").addEventListener("click",()=>setView("database"));
  $("navSandbox").addEventListener("click",()=>setView("sandbox"));
  $("refreshBtn").addEventListener("click",refresh);
  $("autoRefresh").addEventListener("change",updateTimer);
  $("sandboxSearch").addEventListener("input",renderSandboxRows);
  $("sandboxSort").addEventListener("change",renderSandboxRows);
  setInterval(()=>{
    if(!document.hidden && state.active==="sandbox"&&state.snapshot){
      document.querySelectorAll("[data-sandbox-time]").forEach(item=>{
        const record=state.snapshot.sandboxes?.sandboxes?.find(
          entry=>entry.id===item.dataset.sandboxTime
        );
        if(record)item.textContent=lifetime(record);
      });
      document.querySelectorAll("[data-sandbox-expiry]").forEach(item=>{
        const record=state.snapshot.sandboxes?.sandboxes?.find(
          entry=>entry.id===item.dataset.sandboxExpiry
        );
        if(record)item.textContent=ttlText(record);
      });
    }
  },1000);
  updateTimer();
  refresh();
})();
