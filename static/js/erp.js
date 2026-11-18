(function(){
  window.erp = window.erp || {};
  erp.esc = function(s){ return (s??'').toString().replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); };
  erp.repBadge = function(role){
    var value=(role??'').toString().trim().toLowerCase();
    return ['representative','rep','class representative','class rep'].indexOf(value)>=0 ? ' <span class="badge bg-primary" title="Representative">Rep</span>' : '';
  };
  erp.repText = function(role){
    var value=(role??'').toString().trim().toLowerCase();
    return ['representative','rep','class representative','class rep'].indexOf(value)>=0 ? 'Rep ' : '';
  };

  erp.toast = function(msg, type){
    var host=document.getElementById('erpToastHost');
    if(!host){ host=document.createElement('div'); host.id='erpToastHost'; document.body.appendChild(host); }
    var el=document.createElement('div'); el.className='erp-toast '+(type||''); el.textContent=msg; host.appendChild(el);
    setTimeout(function(){ el.remove(); }, 3400);
  };
  erp.fetchJSON = async function(url, opts){
    opts = opts || {}; opts.credentials = opts.credentials || 'same-origin';
    if(opts.body && !(opts.body instanceof FormData) && typeof opts.body === 'object'){ opts.headers=Object.assign({'Content-Type':'application/json'}, opts.headers||{}); opts.body=JSON.stringify(opts.body); }
    const r = await fetch(url, opts); let d={}; try{ d=await r.json(); }catch(_){ d={error:'Non-JSON response (HTTP '+r.status+')'}; }
    if(!r.ok || d.ok===false) throw Object.assign(new Error(d.error||('HTTP '+r.status)), {data:d,status:r.status});
    return d;
  };
  erp.setLoading = function(el, on, text){ if(!el) return; if(on){ el.dataset._prev=el.innerHTML; el.innerHTML='<span class="erp-loading">'+erp.esc(text||'Loading...')+'</span>'; } else if(el.dataset._prev!=null){ el.innerHTML=el.dataset._prev; delete el.dataset._prev; } };
})();
