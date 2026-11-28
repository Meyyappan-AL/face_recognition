// Student ERP dashboard: overview, date x 7-period grid, absent audit, marking log, OD modal
document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const esc = s => (s ?? '').toString().replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const msg = (t, c='text-muted') => { const e=$('dashMsg'); if(e){ e.className='small mb-2 '+c; e.textContent=t; } };

  let selectedSemester = null;
  let lastOverview = null;
  let todayIst = '';

  function stClass(st){
    if (st==='Present') return 'st-Present';
    if (st==='Absent') return 'st-Absent';
    if (st==='N') return 'st-N';
    if (st==='OD') return 'st-OD';
    if (st==='Late') return 'st-Late';
    if (st==='Medical Leave') return 'st-MedicalLeave';
    if (st==='NA') return 'st-NA';
    if (st==='-') return 'st-Unmarked';
    return 'st-NotMarked';
  }
  function stLabel(st){
    if (st==='Present') return 'P';
    if (st==='Absent') return 'A';
    if (st==='N') return 'N';
    if (st==='OD') return 'OD';
    if (st==='Late') return 'L';
    if (st==='Medical Leave') return 'ML';
    if (st==='NA') return '';
    if (st==='-') return '-';
    return '?';
  }
  function fmtDate(iso){
    if(!iso) return '-';
    const d=new Date(iso+'T00:00:00');
    if(isNaN(d)) return iso;
    return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'});
  }

  function renderGoal(o){
    const el=$('goalResult'); const targetEl=$('goalTarget');
    if(!el || !targetEl) return;
    const target=Math.max(0,Math.min(100,Number(targetEl.value)||0));
    const attended=Number(o.present_periods||0)+Number(o.late_periods||0)+Number(o.od_periods||0)+Number(o.medical_periods||0);
    const total=Number(o.total_periods_marked||0);
    if(!target || !total){ el.textContent='Not enough marked periods yet for a goal estimate.'; return; }
    const pct=(attended*100/total);
    if(pct>=target){
      const canMiss=Math.max(0,Math.floor(attended*100/target)-total);
      el.innerHTML=`You are at <strong>${pct.toFixed(2)}%</strong>. You can miss up to <strong>${canMiss}</strong> more counted period(s) and stay at/above <strong>${target}%</strong>.`;
    } else if(target>=100){
      el.innerHTML=`You are at <strong>${pct.toFixed(2)}%</strong>. Attend all remaining counted periods and every future class to reach <strong>100%</strong>.`;
    } else {
      const need=Math.ceil((target*total-100*attended)/(100-target));
      el.innerHTML=`You are at <strong>${pct.toFixed(2)}%</strong>. Attend the next <strong>${Math.max(0,need)}</strong> counted period(s) consecutively to reach <strong>${target}%</strong>.`;
    }
  }

  function renderToday(todayClasses,nextClass){
    const t=$('todayClasses'), n=$('nextClass');
    if(t){
      t.innerHTML=todayClasses.length?todayClasses.map(x=>`<div class="d-flex justify-content-between gap-2 border-bottom py-1"><span>P${esc(x.period)} ${esc(x.subject)}</span><span class="badge bg-light text-dark border">${esc(x.status||'-')}</span></div>`).join(''):'<span class="text-muted">No classes listed for today.</span>';
    }
    if(n){
      if(!nextClass){ n.innerHTML='<span class="text-muted">No upcoming class day found.</span>'; return; }
      n.innerHTML=`<div class="fw-semibold mb-1">${esc(fmtDate(nextClass.date))} (${esc(nextClass.day||'')})</div>`+
        nextClass.periods.map(x=>`<div class="d-flex justify-content-between gap-2 border-bottom py-1"><span>P${esc(x.period)} ${esc(x.subject)}</span><span class="text-muted">${esc(x.slot||'')}</span></div>`).join('');
    }
  }

  function renderHeatmap(grid){
    const box=$('heatmap'); if(!box) return;
    const months={};
    grid.forEach(row=>{
      const dt=new Date(row.date+'T00:00:00'); if(isNaN(dt)) return;
      const key=`${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}`;
      const label=dt.toLocaleDateString('en-GB',{month:'short',year:'numeric'});
      const counts={good:0,bad:0,neutral:0,na:0};
      for(let p=1;p<=7;p++){
        const c=(row.periods||{})[String(p)]||{};
        if(c.status==='NA'||!c.scheduled){ counts.na++; continue; }
        if(['Present','Late','OD','Medical Leave'].includes(c.status)) counts.good++;
        else if(['Absent','N'].includes(c.status)) counts.bad++;
        else counts.neutral++;
      }
      let cls='heat-future';
      if(counts.good&&counts.bad) cls='heat-mixed';
      else if(counts.bad) cls='heat-bad';
      else if(counts.good) cls='heat-good';
      const title=`${fmtDate(row.date)}: ${counts.good} credited, ${counts.bad} absent, ${counts.neutral} not marked`;
      if(!months[key]) months[key]={label,items:[]};
      months[key].items.push({date:row.date,day:dt.getDate(),cls,title});
    });
    const keys=Object.keys(months).sort();
    box.innerHTML=keys.length?keys.map(k=>`<div class="heat-month"><div class="heat-title">${esc(months[k].label)}</div><div class="heat-grid">${months[k].items.sort((a,b)=>a.date.localeCompare(b.date)).map(x=>`<div class="heat-cell ${x.cls}" title="${esc(x.title)}">${x.day}</div>`).join('')}</div></div>`).join(''):'<span class="muted-help">No scheduled days in this range.</span>';
  }

  function renderSubjects(subjects){
    const box=$('subjectBreakdown'); if(!box) return;
    box.innerHTML=subjects.length?subjects.map(s=>{
      const pct=Number(s.percentage||0);
      const threshold=Number(lastOverview?.threshold||75);
      const color=pct>=threshold?'bg-success':pct>=Math.max(0,threshold-15)?'bg-warning':'bg-danger';
      return `<div class="subject-row"><div class="d-flex justify-content-between gap-2"><span class="fw-semibold">${esc(s.subject)}</span><span class="badge bg-light text-dark border">${pct}%</span></div><div class="progress mt-1" style="height:5px"><div class="progress-bar ${color}" style="width:${Math.max(0,Math.min(100,pct))}%"></div></div><div class="text-muted" style="font-size:.7rem">${s.credited}/${s.total} credited | ${s.present}P ${s.late}L ${s.od}OD ${s.medical}ML ${s.absent}A</div></div>`;
    }).join(''):'<div class="small text-muted">No marked subject periods yet.</div>';
  }

  async function loadDashboard(semesterId){
    const target=(semesterId===undefined? selectedSemester : semesterId);
    msg('Loading dashboard...');
    try {
      const sel=$('semSelect');
      const opt=sel?.selectedOptions?.[0];
      const semNo=opt?.getAttribute('data-sem-no')||'';
      const batch=opt?.getAttribute('data-batch')||'';
      const params=new URLSearchParams();
      if(semNo) params.set('sem_no',semNo);
      if(batch) params.set('batch_start_year',batch);
      if(!semNo && target) params.set('semester_id',target);
      const qs=params.toString()?('?'+params.toString()):'';
      const r = await fetch('/api/student/dashboard'+qs, {credentials:'same-origin'});
      const d = await r.json().catch(()=>({}));
      if (!r.ok || !d.ok) { msg(d.error || 'Failed to load dashboard.', 'text-danger'); renderFallback(); return; }
      renderDashboard(d); msg('');
    } catch (e) { console.error(e); msg('Dashboard load error: '+(e.message||e), 'text-danger'); renderFallback(); }
  }

  function renderDashboard(d){
    const o=d.overview||{};
    lastOverview=o;
    todayIst=d.today_ist||'';
    selectedSemester = o.semester_id || selectedSemester;
    const range = `${fmtDate(o.from)} - ${fmtDate(o.to)}${o.is_current? ' (ongoing)':''}`;
    $('overviewRange').textContent = `${o.semester_label||'Semester'} | ${range}`;
    $('mSemester').textContent = o.semester_label || '-';
    if($('mSemesterSub')) $('mSemesterSub').textContent = range;
    $('mWork').textContent = o.working_days ?? 0;
    $('mPresent').textContent = o.days_present ?? 0;
    $('mAbsent').textContent = o.days_absent ?? 0;
    $('mPct').textContent = (o.attendance_percentage ?? 0) + '%';
    $('mMarked').textContent = o.total_periods_marked ?? 0;
    $('mScheduled').textContent = o.scheduled_periods ?? 0;
    $('mOd').textContent = o.od_periods ?? 0;
    $('mLate').textContent = o.late_periods ?? 0;
    $('mAbsentP').textContent = o.absent_periods ?? 0;
    $('mNotMarked').textContent = o.not_marked_periods ?? 0;
    if($('overviewDetail')){
      $('overviewDetail').textContent = `Counted periods: ${o.present_periods??0} present, ${o.late_periods??0} late, ${o.od_periods??0} OD, ${o.medical_periods??0} medical, ${o.absent_periods??0} absent (${o.n_periods??0} N). Unmarked: ${o.unmarked_periods??0}; future not marked: ${(o.not_marked_periods??0)-(o.unmarked_periods??0)}.`;
    }
    const targetEl=$('goalTarget');
    if(targetEl && !targetEl.dataset.touched) targetEl.value=String(o.threshold??75);
    renderGoal(o);
    const warn=$('pctWarning');
    if(warn){
      const pct=Number(o.attendance_percentage??0), threshold=Number(o.threshold??75);
      if((o.total_periods_marked||0)>0 && pct<threshold){
        warn.classList.remove('d-none');
        warn.innerHTML=`<strong>Attendance warning:</strong> your overall attendance is <strong>${pct}%</strong>, below the required <strong>${threshold}%</strong>. Meet your class advisor and review the goal calculator.`;
      } else warn.classList.add('d-none');
    }
    const exp=$('exportCsvBtn');
    if(exp && d.export_url) exp.href=d.export_url;
    renderToday(d.today_classes||[], d.next_class||null);
    renderHeatmap(d.grid||[]);
    renderSubjects(d.subjects||[]);

    // semester selector
    const sel=$('semSelect');
    if(sel){
      const opts=d.semesters||[];
      if(sel.dataset.sig!==JSON.stringify(opts.map(x=>x.id))){
        sel.innerHTML=opts.map(s=>`<option value="${s.id}" data-sem-no="${s.sem_no||''}" data-batch="${s.batch_start_year||''}">Sem ${esc(s.sem_no||'?')} | ${esc(s.label)}${s.is_current||s.status==='current'?' (current)':''}${s.start_date?' | '+esc(s.start_date):''}${s.end_date?' to '+esc(s.end_date):''}</option>`).join('');
        sel.dataset.sig=JSON.stringify(opts.map(x=>x.id));
      }
      if(o.semester_id) sel.value=String(o.semester_id);
    }

    // calendar (collapsed by default)
    const cal = d.calendar_upcoming||[];
    $('calendarStrip').innerHTML = cal.length ? cal.slice(0,8).map(c=>`<span class="badge bg-light text-dark border cal-chip" title="${esc(c.notes||'')}">${esc(fmtDate(c.date))}${c.end_date&&c.end_date!==c.date?' to '+esc(fmtDate(c.end_date)):''} - ${esc(c.title)} <span class="text-muted">(${esc(c.type)})</span></span>`).join('') : '<span class="muted-help">No upcoming calendar events from admin.</span>';

    // grid
    const grid=d.grid||[]; const body=$('gridBody');
    if (!grid.length) { body.innerHTML = '<tr><td colspan="8" class="small text-muted">No scheduled periods in this semester yet.</td></tr>'; }
    else {
      body.innerHTML = grid.map(row => {
        const calNote = (row.calendar||[]).map(c=>esc(c.title)).join(', ');
        let tds = `<th class="date-col"><div>${esc(fmtDate(row.date))}</div><div class="text-muted" style="font-size:.68rem">${esc(row.day||'')}${calNote? ' - '+calNote:''}</div></th>`;
        for (let p=1;p<=7;p++){
          const c=(row.periods||{})[String(p)]||{status:'NA',period:p};
          const payload = encodeURIComponent(JSON.stringify(c));
          const label=stLabel(c.status);
          const extra=(c.status==='-'?'unmarked':c.status||'').replace(/"/g,'&quot;');
          const title = `${fmtDate(row.date)} P${p} ${c.subject_display||''} - ${extra}`.replace(/"/g,'&quot;');
          const dis=c.status==='NA'?'disabled':'';
          tds += `<td><button type="button" class="st-btn ${stClass(c.status)}" data-log="${payload}" ${dis} title="${esc(title)}">${label}</button></td>`;
        }
        return `<tr>${tds}</tr>`;
      }).join('');
      body.querySelectorAll('[data-log]').forEach(btn=>btn.addEventListener('click', ()=>openLog(btn.getAttribute('data-log'))));
    }

    // timetable
    const tt=d.timetable||{}; const sched=tt.schedule||{}; const days=['MON','TUE','WED','THU','FRI','SAT'];
    const slots=tt.slots||[]; const slotMap={}; slots.forEach(s=>slotMap[String(s.period)]=s);
    if($('ttMeta')) $('ttMeta').textContent=(tt.class_name||'')+(tt.advisor&&tt.advisor.initials?(' | Advisor: '+tt.advisor.initials):'');
    if($('ttBody')){
      const hasTT = days.some(dk=>(sched[dk]||[]).length);
      $('ttBody').innerHTML = hasTT ? days.map(dk=>{
        let tds=`<th class="date-col">${dk}</th>`;
        for(let p=1;p<=7;p++){
          const cell=(sched[dk]||[]).find(x=>String(x.period)===String(p))||{};
          const sl=slotMap[String(p)]||{};
          const label=cell.display||cell.subject||'—';
          tds+=`<td title="${esc((sl.start||'')+'-'+(sl.end||''))} ${esc((cell.staff||[]).join(', '))}"><div style="font-size:.68rem;line-height:1.1">${esc(label)}</div></td>`;
        }
        return `<tr>${tds}</tr>`;
      }).join('') : '<tr><td colspan="8" class="small text-muted">Timetable not configured for this class.</td></tr>';
    }

    // audit
    const audit=d.absent_audit||[]; const a=$('auditList');
    a.innerHTML = audit.length ? audit.map(x=>{
      const src=x.status==='N'?'Marked N (absence)':(x.marked_by?'Marked by '+esc(x.marked_by):'Recorded');
      return `<div class="p-2 mb-2 bg-white rounded audit-item small"><div><strong>${esc(fmtDate(x.date))}</strong> - P${esc(x.period)} - ${esc(x.subject||'')}</div><div class="text-muted">${esc(x.status||'Absent')} | ${esc(src)}</div></div>`;
    }).join('') : '<div class="small text-muted">No absent periods in this range.</div>';
  }

  function renderFallback(){
    // keep page usable; old APIs still exist
    $('gridBody').innerHTML = '<tr><td colspan="8" class="small text-danger">Dashboard API unavailable. Refresh after server restart.</td></tr>';
  }

  function openLog(encoded){
    let c={}; try{ c=JSON.parse(decodeURIComponent(encoded)); }catch(e){}
    const status=c.status||'';
    const na=status==='NA', notMarked=status==='Not Marked', unmarked=status==='-';
    const badge=status==='Present'?'bg-success':['Absent','N'].includes(status)?'bg-danger':status==='OD'?'bg-primary':status==='Late'?'bg-warning text-dark':status==='Medical Leave'?'bg-info text-dark':'bg-secondary';
    const rows=[];
    rows.push(['Date', esc(fmtDate(c.date))]);
    rows.push(['Period', 'P'+esc(c.period||'-')+(c.subject_display?' - '+esc(c.subject_display):'')]);
    rows.push(['Class', esc(c.class_name||'-')]);
    if(c.slot) rows.push(['Slot time', esc(c.slot)]);
    if(!na){
      rows.push(['Method', esc(c.method_label||c.source||'Not recorded')]);
      if(c.marked_by) rows.push(['Marked by', esc(c.marked_by)]);
      if(c.updated_at||c.submitted_at) rows.push(['Recorded / updated', esc(c.updated_at||c.submitted_at)]);
      if(c.confidence_label||c.confidence!=null) rows.push(['Match confidence', esc(c.confidence_label||c.confidence)]);
    }
    let note='Recorded from staff period attendance, an approved OD workflow, or approved medical leave.';
    if(na) note='No scheduled class for this period.';
    else if(notMarked) note='This period is scheduled but its slot has not started yet.';
    else if(unmarked) note='This period is unmarked ("-"). It is excluded from totals until staff records an explicit status.';
    else if(status==='N') note='N is an absence and is counted as absent in percentages and reports.';
    $('logBody').innerHTML = `
      <div class="mb-2"><span class="badge ${badge}">${esc(status||'N/A')}</span></div>
      <table class="table table-sm small mb-0">
        ${rows.map(r=>`<tr><th style="width:42%">${r[0]}</th><td>${r[1]}</td></tr>`).join('')}
      </table>
      <div class="alert alert-light border mt-2 mb-0 small">${esc(note)}</div>`;
    if (window.bootstrap) new bootstrap.Modal($('logModal')).show();
  }

  // OD modal
  const odModalEl = $('odModal');
  const openOd = (e)=>{ if(e) e.preventDefault(); loadOdHistory(); setOdDates(); if(window.bootstrap) new bootstrap.Modal(odModalEl).show(); };
  $('openOdBtn')?.addEventListener('click', openOd);
  $('openOdNav')?.addEventListener('click', openOd);
  $('scopeType')?.addEventListener('change', ()=>{ $('specificBox').style.display = $('scopeType').value==='specific' ? 'block':'none'; });
  function setOdDates(){ const t=todayIst||new Date().toISOString().slice(0,10); if($('odFrom') && !$('odFrom').value) $('odFrom').value=t; if($('odTo') && !$('odTo').value) $('odTo').value=t; }
  function odMsg(t,c='text-muted'){ const e=$('odMsg'); if(e){e.className='small mt-2 '+c; e.textContent=t;} }

  async function loadOdHistory(){
    const box=$('odHistory'); if(!box) return; box.innerHTML='<div class="small text-muted">Loading...</div>';
    try{
      const r=await fetch('/api/od/my',{credentials:'same-origin'}); const d=await r.json().catch(()=>({}));
      const rows=d.requests||[];
      box.innerHTML = rows.length ? rows.map(x=>{
        const rej=(x.advisor_decision==='rejected'?x.advisor_note:'')||(x.hod_decision==='rejected'?x.hod_note:'')||(x.hod_decision==='cancelled'?x.hod_note:'')||'';
        const hodDisp = x.hod_decision_display || ((x.advisor_decision==='rejected')?'NIL':(x.hod_decision||'-'));
        const bc = x.status==='od_approved'?'success':x.status==='cancelled'?'danger':'warning text-dark';
        return `<div class="border rounded p-2 mb-2 small bg-white"><div class="d-flex justify-content-between gap-2"><strong>#${x.id} ${esc(x.from_date||'')}${x.to_date&&x.to_date!==x.from_date?'?'+esc(x.to_date):''}</strong><span class="badge bg-${bc}">${esc(x.status||'')}</span></div><div class="text-muted">${esc(x.status_label||'')}</div><div class="small">Advisor: ${esc(x.advisor_decision||'-')} | HOD: ${esc(hodDisp)}</div>${rej?`<div class="text-danger">Message: ${esc(rej)}</div>`:''}<div>${esc((x.reason||'').slice(0,140))}</div></div>`;
      }).join('') : '<div class="small text-muted">No OD requests yet.</div>';
    }catch(e){ box.innerHTML='<div class="small text-danger">Could not load OD history.</div>'; }
  }
  $('reloadOdHistory')?.addEventListener('click', loadOdHistory);

  $('odForm')?.addEventListener('submit', async e=>{
    e.preventDefault(); odMsg('Submitting...');
    try{
      if($('scopeType').value==='specific'){
        const ps=[...document.querySelectorAll('.period-cb:checked')].map(x=>x.value);
        if(!ps.length){ odMsg('Select at least one period.','text-danger'); return; }
        $('periodsJson').value=ps.join(',');
      } else $('periodsJson').value='';
      const fd=new FormData(e.target);
      const r=await fetch('/api/od/request',{method:'POST',body:fd,credentials:'same-origin'});
      let d={}; try{ d=await r.json(); }catch(_){ d={error:'Server returned non-JSON response (HTTP '+r.status+')'}; }
      if(r.ok && d.ok){ odMsg(d.message||'Submitted','text-success'); e.target.reset(); $('specificBox').style.display='none'; setOdDates(); loadOdHistory(); setTimeout(loadDashboard, 700); }
      else odMsg((d && d.error) ? d.error : ('Submit failed (HTTP '+r.status+')'),'text-danger');
    }catch(err){ odMsg('Submit error: '+(err&&err.message?err.message:err),'text-danger'); }
  });

  // In-app alerts, read state and optional Web Push
  async function loadNotifications(){
    try{
      const r=await fetch('/student_notifications',{credentials:'same-origin'});
      const d=await r.json().catch(()=>({}));
      const logs=d.notifications||d.attendance_log||[];
      const alerts=logs.filter(n=>n.is_alert || n.alert_id);
      const unread=Number(d.unread||0);
      const recent=alerts.find(n=>!n.read);
      const alertEl=$('notifAlert'); const badge=$('notifBadge'); const list=$('notifList');
      if(unread>0){ if(badge){ badge.textContent=String(unread>99?'99+':unread); badge.classList.remove('d-none'); } }
      else { if(badge) badge.classList.add('d-none'); }
      if(recent && alertEl){
        alertEl.classList.remove('d-none');
        alertEl.innerHTML=`<strong>${esc(recent.label||recent.title||'Attendance alert')}:</strong> ${esc(recent.body||recent.message||'')} <span class="text-muted">(${esc((recent.timestamp||'').slice(0,19).replace('T',' '))})</span>`;
      } else if(alertEl){ alertEl.classList.add('d-none'); }
      if(list){
        list.innerHTML=alerts.length?alerts.map(n=>`<div class="border rounded p-2 mb-2 ${n.read?'bg-white':'bg-warning bg-opacity-10'}"><div class="d-flex justify-content-between gap-2"><div class="fw-semibold">${esc(n.label||n.title||'Alert')}</div>${n.read?'<span class="badge bg-secondary">Read</span>':'<span class="badge bg-danger">Unread</span>'}</div><div class="text-muted mt-1">${esc(n.body||n.message||'')}</div><div class="text-muted small mt-1">${esc((n.timestamp||'').slice(0,19).replace('T',' '))}</div></div>`).join(''):'<div class="text-muted">No alerts yet.</div>';
      }
    }catch(e){ /* non-fatal */ }
  }

  async function markAllRead(){
    const btn=$('markAllReadBtn'); if(btn) btn.disabled=true;
    try{
      const d=await erp.fetchJSON('/api/student/alerts/read_all',{method:'POST',body:{}});
      if(window.erp) erp.toast((d.marked||0)+' alerts marked read','success');
      await loadNotifications();
    }catch(e){ if(window.erp) erp.toast(e.message||'Could not mark alerts read','danger'); }
    finally{ if(btn) btn.disabled=false; }
  }

  function urlBase64ToUint8Array(base64String){
    const padding='='.repeat((4-base64String.length%4)%4);
    const base64=(base64String+padding).replace(/-/g,'+').replace(/_/g,'/');
    const raw=window.atob(base64); const out=new Uint8Array(raw.length);
    for(let i=0;i<raw.length;i++) out[i]=raw.charCodeAt(i);
    return out;
  }
  function pushMsg(t,type='light'){
    const e=$('pushStatus'); if(!e) return;
    if(!t){ e.classList.add('d-none'); return; }
    e.className='alert alert-'+type+' border py-2 small'; e.textContent=t;
  }
  async function loadPushState(){
    const btn=$('enablePushBtn');
    if(!('serviceWorker' in navigator)||!('PushManager' in window)){ if(btn){btn.disabled=true;btn.textContent='Push unsupported';} return; }
    try{
      const reg=await navigator.serviceWorker.register('/sw.js');
      const sub=await reg.pushManager.getSubscription();
      if(btn){ btn.textContent=sub?'Push enabled':'Enable push alerts'; }
      if(sub) pushMsg('Browser push is enabled on this device.', 'success');
    }catch(e){ if(btn) btn.disabled=true; pushMsg('Push setup unavailable: '+(e.message||e),'warning'); }
  }
  async function enablePush(){
    const btn=$('enablePushBtn'); if(btn) btn.disabled=true;
    try{
      if(!('serviceWorker' in navigator)||!('PushManager' in window)) throw new Error('This browser does not support Web Push.');
      if(Notification.permission==='denied') throw new Error('Notification permission is blocked. Allow notifications in browser settings.');
      if(Notification.permission!=='granted'){ const p=await Notification.requestPermission(); if(p!=='granted') throw new Error('Notification permission was not granted.'); }
      const kd=await fetch('/api/push/public_key',{credentials:'same-origin'}).then(r=>r.json()).catch(()=>({}));
      if(!kd.ok || !kd.publicKey) throw new Error('Push is not configured by admin (VAPID keys missing).');
      const reg=await navigator.serviceWorker.ready;
      let sub=await reg.pushManager.getSubscription();
      if(!sub) sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:urlBase64ToUint8Array(kd.publicKey)});
      await erp.fetchJSON('/api/push/subscribe',{method:'POST',body:{subscription:sub.toJSON()}});
      pushMsg('Browser push enabled on this device.','success');
      if(btn) btn.textContent='Push enabled';
      if(window.erp) erp.toast('Push alerts enabled','success');
    }catch(e){ pushMsg(e.message||'Could not enable push.','danger'); if(window.erp) erp.toast(e.message||'Push setup failed','danger'); }
    finally{ if(btn) btn.disabled=false; }
  }
  $('openNotifBtn')?.addEventListener('click', ()=>{ loadNotifications(); loadPushState(); if(window.bootstrap) new bootstrap.Modal($('notifModal')).show(); });
  $('markAllReadBtn')?.addEventListener('click', markAllRead);
  $('enablePushBtn')?.addEventListener('click', enablePush);

  // nav active state (simple in-page)
  document.querySelectorAll('.erp-nav [data-target]').forEach(a=>a.addEventListener('click', ()=>{
    document.querySelectorAll('.erp-nav .nav-link').forEach(n=>n.classList.remove('active')); a.classList.add('active');
  }));

  $('semSelect')?.addEventListener('change', e=>{ selectedSemester=e.target.value; loadDashboard(selectedSemester); });
  $('goalTarget')?.addEventListener('input', e=>{ e.target.dataset.touched='1'; renderGoal(lastOverview||{}); });
  $('refreshDash')?.addEventListener('click', ()=>{ loadDashboard(); loadNotifications(); });
  loadDashboard(); loadNotifications();
});
