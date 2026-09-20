"use strict";
/* Browser contacts only /admin/api, never raw PostgreSQL or Docker. */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {overview:null, preview:null, view:"overview", busy:false};
  const views = {
    overview:["运行资产总览","检查构建、执行和发布状态，追踪需要处理的资产。"],
    environments:["运行环境","查看构建环境与构建任务，并对环境执行清理预检。"],
    runner:["Runner 执行","租约、执行记录和幂等状态均来自 Runner 数据库。"],
    publish:["发布记录","检查发布变体与发布任务的实际状态。"],
    requests:["清理申请","核对关联引用并留存申请；不直接删除物理资源。"],
    notes:["资产备注","管理运维备注，不修改业务服务的数据。"],
    audit:["操作审计","追踪备注和清理申请的增删改动作。"],
  };
  const el=(tag,cls="",value)=>{
    const x=document.createElement(tag);
    if(cls)x.className=cls;
    if(value!==undefined&&value!==null)x.textContent=String(value);
    return x;
  };
  const number=(x)=>typeof x==="number"?new Intl.NumberFormat("zh-CN").format(x):"—";
  const date=(value)=>{
    if(!value)return"—";
    const d=new Date(value);
    return Number.isNaN(d.valueOf())?"—":new Intl.DateTimeFormat("zh-CN",{
      month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",
    }).format(d);
  };
  const code=(value)=>el("span","mono",value??"—");
  const tag=(value)=>{
    const x=el("span","chip "+String(value||"").toLowerCase(),value||"—");
    return x;
  };
  const asText=(value,max=150)=>value===null||value===undefined?"—":String(value).slice(0,max);
  function feedback(msg,kind=""){
    $("feedback").textContent=msg;
    $("feedback").className="feedback "+kind;
  }
  function show(view){
    if(!views[view])return;
    state.view=view;
    for(const name of Object.keys(views)){
      $(`view-${name}`).classList.toggle("hidden",name!==view);
    }
    document.querySelectorAll(".nav").forEach(button=>{
      button.classList.toggle("active",button.dataset.view===view);
      if(button.dataset.view===view)button.setAttribute("aria-current","page");
      else button.removeAttribute("aria-current");
    });
    $("pageTitle").textContent=views[view][0];
    $("pageHint").textContent=views[view][1];
    $("pageTitle").focus({preventScroll:true});
    window.scrollTo({top:0,behavior:"instant"});
  }
  document.querySelectorAll(".nav").forEach(button=>{
    button.addEventListener("click",()=>show(button.dataset.view));
  });

  async function api(method,path,body){
    const options={method,credentials:"same-origin",cache:"no-store",
      headers:{Accept:"application/json"}};
    if(body!==undefined){
      options.headers["Content-Type"]="application/json";
      options.headers["X-Admin-Request"]="1";
      options.body=JSON.stringify(body);
    }
    const response=await fetch(path,options);
    const text=await response.text();
    let data;
    try{data=JSON.parse(text)}catch{data={detail:"响应不是 JSON"}}
    if(!response.ok)throw new Error(
      typeof data.detail==="string"?data.detail:`请求失败 HTTP ${response.status}`
    );
    return data;
  }
  function rows(hostId,data,mapper,count){
    const host=$(hostId);host.replaceChildren();
    if(!data||data.status!=="ok"){
      const row=el("tr"),cell=el("td","empty",
        data?.message||"数据源不可用");
      cell.colSpan=count;row.append(cell);host.append(row);return;
    }
    const values=data.rows||[];
    if(!values.length){
      const row=el("tr"),cell=el("td","empty","暂无记录");
      cell.colSpan=count;row.append(cell);host.append(row);return;
    }
    for(const item of values){
      const tr=el("tr");
      mapper(item).forEach(content=>{
        const td=el("td");
        if(content instanceof Node)td.append(content);
        else td.textContent=asText(content,320);
        tr.append(td);
      });
      host.append(tr);
    }
  }
  function inspectButton(id){
    const button=el("button","button secondary small","预检");
    button.type="button";
    button.addEventListener("click",async()=>{
      show("requests");
      $("environmentSelect").value=id;
      await inspect(id);
    });
    return button;
  }
  function environmentActions(item){
    const group=el("div","table-actions");
    group.append(inspectButton(item.id));
    if(item.status==="FAILED" && state.overview?.capabilities?.buildRetry){
      const retry=el("button","button secondary small","重试构建");
      retry.addEventListener("click",()=>confirmAction(
        "重试失败的构建",
        `确认通过 Build Service 重试 ${item.envKey}？服务端将重新检查状态并创建新的构建任务。`,
        async()=>{
          try{
            const result=await api("POST",
              `/admin/api/environments/${encodeURIComponent(item.envKey)}/retry`,{});
            feedback(`Build Service 已接收重试：${result.envKey} · ${result.status}`,"success");
            await refresh();show("environments");
          }catch(error){feedback(error.message,"error")}
        }
      ));
      group.append(retry);
    }
    return group;
  }
  function buildInventory(data){
    const inventory=data.inventory||{};
    const environments=inventory.environments||{};
    const leases=inventory.leases||{};
    const jobs=inventory.buildJobs||{};
    const metadata=data.management||{};

    $("envCount").textContent=environments.status==="ok"?
      number(environments.rows.length):"—";
    $("leaseCount").textContent=leases.status==="ok"?
      number(leases.rows.filter(r=>r.expiresAt&&new Date(r.expiresAt)>new Date()).length):"—";
    $("failedBuildCount").textContent=jobs.status==="ok"?
      number(jobs.rows.filter(r=>r.status==="FAILED").length):"—";
    $("draftCount").textContent=metadata.status==="ok"?
      number(metadata.requests.filter(r=>r.status==="DRAFT").length):"—";

    const cardHost=$("riskCards");cardHost.replaceChildren();
    const risks=[
      ["READY 缺少镜像",
        environments.status==="ok"?
          environments.rows.filter(r=>r.status==="READY"&&!r.imageRef).length:null,
        "high"],
      ["过期租约（样本）",
        leases.status==="ok"?
          leases.rows.filter(r=>r.expiresAt&&new Date(r.expiresAt)<=new Date()).length:null,
        "warning"],
      ["未完成构建（样本）",
        jobs.status==="ok"?
          jobs.rows.filter(r=>["BUILDING","VERIFYING"].includes(r.status)).length:null,
        "warning"],
    ];
    for(const [name,val,level] of risks){
      const card=el("div","risk-card "+level);
      card.append(el("span","",name),el("strong","",number(val)));
      cardHost.append(card);
    }
    rows("envRows",environments,item=>[
      code(item.envKey),tag(item.status),
      code(item.imageRef),number(item.packageCount),date(item.updatedAt),
      environmentActions(item),
    ],6);
    $("envCoverage").textContent=environments.status==="ok"?
      `已载入 ${environments.rows.length} 条（上限 100）`:"暂无数据";
    rows("buildJobRows",jobs,item=>[
      code(item.id),code(item.envId),tag(item.status),date(item.createdAt),
    ],4);
    rows("leaseRows",inventory.leases,item=>[
      code(item.id),asText(item.tenantId),code(item.releaseId),
      date(item.expiresAt),
    ],4);
    rows("runRows",inventory.runs,item=>[
      code(item.id),code(item.releaseId),tag(item.state),
      typeof item.durationMs==="number"?`${item.durationMs} ms`:"—",
      date(item.startedAt),
    ],5);
    rows("idempotencyRows",inventory.idempotency,item=>[
      asText(item.tenantId),code(item.releaseId),tag(item.state),
      number(item.replayCount),date(item.expiresAt),
    ],5);
    rows("variantRows",inventory.variants,item=>[
      code(item.id),asText(item.backend),tag(item.status),
      asText(item.publishedRef,220),date(item.updatedAt),
    ],5);
    rows("publishJobRows",inventory.publishJobs,item=>[
      code(item.id),code(item.variantId),tag(item.status),
      asText(item.artifactRef,220),date(item.createdAt),
    ],5);

    const picker=$("environmentSelect");
    const prior=picker.value;
    picker.replaceChildren(el("option","","请选择运行环境"));
    picker.firstElementChild.value="";
    for(const env of environments.rows||[]){
      const option=el("option","",
        `${env.envKey} · ${env.status} · ${env.id.slice(0,8)}`);
      option.value=env.id;picker.append(option);
    }
    if(Array.from(picker.options).some(o=>o.value===prior))picker.value=prior;
    renderManagement(metadata);
  }
  function resultRow(items){
    const row=el("tr");
    for(const value of items){
      const cell=el("td");
      if(value instanceof Node)cell.append(value);
      else cell.textContent=asText(value,320);
      row.append(cell);
    }
    return row;
  }
  function placeholder(host,cols,msg){
    const row=el("tr"),cell=el("td","empty",msg);
    cell.colSpan=cols;row.append(cell);host.append(row);
  }
  function confirmAction(title,message,callback){
    $("dialogTitle").textContent=title;
    $("dialogText").textContent=message;
    const dialog=$("confirmDialog");
    const confirm=$("confirmActionBtn");
    const cancel=$("cancelActionBtn");
    confirm.onclick=()=>{
      dialog.close();
      callback();
    };
    cancel.onclick=()=>dialog.close();
    dialog.showModal();
  }
  function renderManagement(data){
    const requests=$("requestRows");requests.replaceChildren();
    const list=$("noteList");list.replaceChildren();
    const audit=$("auditRows");audit.replaceChildren();
    if(data.status!=="ok"){
      placeholder(requests,5,data.message||"未配置管理数据库");
      placeholder(audit,4,data.message||"未配置管理数据库");
      list.append(el("div","note-card",data.message||"未配置管理数据库"));
      return;
    }
    if(!data.requests?.length)placeholder(requests,5,"暂无清理申请");
    for(const record of data.requests||[]){
      const btn=el("button","button secondary small","撤销");
      btn.disabled=record.status!=="DRAFT";
      btn.type="button";
      btn.addEventListener("click",()=>confirmAction(
        "撤销清理申请",
        `确认撤销申请 ${record.id}？这不会删除镜像或环境。`,
        async()=>{
          try{
            await api("POST",`/admin/api/cleanup-requests/${record.id}/cancel`,{});
            feedback("申请已撤销。","success");
            await refresh();
          }catch(error){feedback(error.message,"error")}
        },
      ));
      requests.append(resultRow([
        code(record.id),code(record.envKey),tag(record.status),
        asText(record.reason,190),btn,
      ]));
    }
    if(!data.notes?.length)list.append(el("div","note-card","暂无备注"));
    for(const record of data.notes||[]){
      const box=el("div","note-card");
      const title=el("div","note-top");
      title.append(el("strong","",`${record.assetType} · ${record.assetId}`),
                   el("small","",`版本 ${record.version}`));
      box.append(title,el("p","",record.note));
      box.append(el("small","",`更新于 ${date(record.updatedAt)}`));
      const buttons=el("div","note-actions");
      const edit=el("button","button secondary small","编辑");
      edit.addEventListener("click",()=>{
        const textarea=el("textarea");
        textarea.value=record.note;textarea.maxLength=1000;textarea.rows=3;
        box.replaceChildren(title,textarea);
        const actions=el("div","note-actions");
        const save=el("button","button primary small","保存");
        save.addEventListener("click",async()=>{
          try{
            await api("PATCH",`/admin/api/notes/${record.id}`,{
              note:textarea.value,version:record.version,
            });
            feedback("备注已更新。","success");await refresh();
          }catch(error){feedback(error.message,"error")}
        });
        const cancel=el("button","button secondary small","取消");
        cancel.addEventListener("click",()=>renderManagement(data));
        actions.append(cancel,save);box.append(actions);
      });
      const remove=el("button","button secondary small","删除");
      remove.addEventListener("click",()=>confirmAction(
        "删除资产备注",
        `仅删除 ${record.assetId} 的控制台备注，不删除业务资产。审计记录会保留。`,
        async()=>{
          try{
            await api("DELETE",`/admin/api/notes/${record.id}`,{
              version:record.version,
            });
            feedback("备注已删除，审计已记录。","success");await refresh();
          }catch(error){feedback(error.message,"error")}
        },
      ));
      buttons.append(edit,remove);box.append(buttons);list.append(box);
    }
    if(!data.audit?.length)placeholder(audit,4,"暂无操作记录");
    for(const item of data.audit||[]){
      audit.append(resultRow([
        date(item.createdAt),asText(item.actor),
        asText(item.action),code(item.assetId),
      ]));
    }
  }
  async function inspect(envId){
    const id=envId||$("environmentSelect").value;
    if(!id){feedback("请先选择环境。","error");return}
    $("previewResult").classList.add("hidden");
    $("cleanupForm").classList.add("hidden");
    state.preview=null;
    try{
      const data=await api("GET",`/admin/api/environments/${encodeURIComponent(id)}/preview`);
      state.preview=data;
      const area=$("previewResult");area.replaceChildren();
      area.append(el("h3","","清理预检结果"));
      if(!data.found){
        area.append(el("p","","运行环境未找到。"));
      }else{
        const env=data.environment;
        const dl=el("dl");
        for(const [label,value] of [
          ["环境",env.envKey],["状态",env.status],
          ["镜像",env.imageRef],["关联别名",number(data.aliasCount)],
          ["进行中的 Build Job",number(data.activeBuildJobs)],
          ["关联的 Publish Release",number(data.publishedReferences)],
          ["有效 Runner 租约",number(data.activeLeaseReferences)],
        ]){
          const row=el("div");
          row.append(el("dt","",label),el("dd","",value??"—"));
          dl.append(row);
        }
        area.append(dl);
        if(data.blockingReasons?.length){
          const box=el("div","blocked");
          box.append(el("strong","","存在阻断项"));
          const list=el("ul");
          for(const reason of data.blockingReasons)list.append(el("li","",reason));
          box.append(list);area.append(box);
        }else {
          area.append(el("div","good",
            "数据库引用预检未发现明确阻断，但物理资源与并发状态仍需额外核验。"));
        }
        if(data.warnings?.length){
          const warn=el("div","warning");
          const list=el("ul");
          for(const msg of data.warnings)list.append(el("li","",msg));
          warn.append(list);area.append(warn);
        }
        $("cleanupForm").classList.remove("hidden");
        $("cleanupConfirm").value="";
        $("cleanupReason").value="";
        $("cleanupConfirm").placeholder=`输入：${env.envKey}`;
      }
      area.classList.remove("hidden");
      feedback("预检已完成。保存申请不会删除资源。","success");
    }catch(error){feedback(error.message,"error")}
  }
  async function refresh(){
    if(state.busy)return;
    state.busy=true;$("loading").classList.remove("hidden");$("refreshBtn").disabled=true;
    try{
      const data=await api("GET","/admin/api/overview");
      state.overview=data;buildInventory(data);
      if(data.management?.status!=="ok"){
        feedback("管理元数据库未就绪，备注与清理申请不可写；业务资产查询仍可使用。");
      }else {
        $("feedback").classList.add("hidden");
      }
    }catch(error){feedback(error.message,"error")}
    finally{state.busy=false;$("loading").classList.add("hidden");$("refreshBtn").disabled=false}
  }
  $("refreshBtn").addEventListener("click",refresh);
  $("previewBtn").addEventListener("click",()=>inspect());
  $("envSearch").addEventListener("input",()=>{
    const source=state.overview?.inventory?.environments;
    if(!source)return;
    const query=$("envSearch").value.trim().toLowerCase();
    rows("envRows",{...source,rows:source.rows.filter(r=>
      [r.envKey,r.status,r.imageRef].join(" ").toLowerCase().includes(query)
    )},item=>[code(item.envKey),tag(item.status),code(item.imageRef),
      number(item.packageCount),date(item.updatedAt),environmentActions(item)],6);
  });
  $("cleanupForm").addEventListener("submit",async(event)=>{
    event.preventDefault();
    const preview=state.preview;
    if(!preview?.found){feedback("请先完成清理预检。","error");return}
    try{
      const response=await api("POST","/admin/api/cleanup-requests",{
        environmentId:preview.environmentId,
        reason:$("cleanupReason").value,
        confirmation:$("cleanupConfirm").value,
      });
      feedback(`清理申请 ${response.id} 已保存。尚未执行任何物理或业务数据库删除。`,"success");
      state.preview=null;
      $("cleanupForm").classList.add("hidden");
      await refresh();show("requests");
    }catch(error){feedback(error.message,"error")}
  });
  $("noteForm").addEventListener("submit",async(event)=>{
    event.preventDefault();
    try{
      await api("POST","/admin/api/notes",{
        assetType:$("noteType").value,assetId:$("noteAsset").value,
        note:$("noteText").value,
      });
      $("noteForm").reset();
      await refresh();show("notes");feedback("备注已创建。","success");
    }catch(error){feedback(error.message,"error")}
  });
  show("overview");refresh();
})();
