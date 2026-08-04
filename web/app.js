const $ = (selector) => document.querySelector(selector);
const results = $('#results');
const toast = (message) => { const el=$('#toast'); el.textContent=message; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); };

fetch('/api/health').then(async response => {
  const data=await response.json(); if(!response.ok) throw new Error(data.error);
  $('#connection').textContent=data.version; $('#connection').classList.add('ok');
}).catch(error => { $('#connection').textContent='CLI unavailable'; $('#connection').title=error.message; });

$('#search-form').addEventListener('submit', async event => {
  event.preventDefault(); const params=new URLSearchParams(new FormData(event.currentTarget));
  $('#results-title').textContent=`Searching for “${params.get('q')}”…`; results.innerHTML='<div class="empty"><p>Searching your catalog…</p></div>';
  try { const response=await fetch(`/api/search?${params}`); const data=await response.json(); if(!response.ok) throw new Error(data.error); render(data.results || []); }
  catch(error){ results.innerHTML=`<div class="empty"><p>${escapeHTML(error.message)}</p></div>`; $('#results-title').textContent='Search unavailable'; }
});

function render(items){ $('#results-title').textContent=`${items.length} result${items.length===1?'':'s'}`; if(!items.length){results.innerHTML='<div class="empty"><p>No matches found. Try a broader search.</p></div>';return}
  results.innerHTML=items.map(item=>{const torrents=item.torrents||item.releases||[]; const title=item.title||item.name||'Untitled'; const rating=item.ratingImdb||item.ratingTmdb||item.rating; return `<article class="card"><div class="meta">${escapeHTML([item.contentType||item.type,item.year,rating&&`★ ${rating}`].filter(Boolean).join(' · '))}</div><h3>${escapeHTML(title)}</h3><p>${escapeHTML(item.overview||item.description||'')}</p><div class="releases">${torrents.slice(0,3).map(release=>{const hash=release.infoHash||release.info_hash||'';return `<div class="release"><b>${escapeHTML(release.quality||release.resolution||'Release')}</b><span>${escapeHTML(String(release.seeders??''))}${release.seeders!=null?' seeds':''}</span>${hash?`<button data-hash="${escapeHTML(hash)}">Download</button>`:''}</div>`}).join('')}</div></article>`}).join('');
}
results.addEventListener('click',async event=>{const button=event.target.closest('[data-hash]');if(!button)return;button.disabled=true;button.textContent='Starting…';try{const response=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({infoHash:button.dataset.hash})});const data=await response.json();if(!response.ok)throw new Error(data.error);button.textContent='Started';toast('Download started in unarr');}catch(error){button.disabled=false;button.textContent='Download';toast(error.message)}});
$('#status-button').addEventListener('click',async()=>{const dialog=$('#status-dialog');dialog.showModal();$('#status-output').textContent='Loading…';try{const response=await fetch('/api/status');const data=await response.json();if(!response.ok)throw new Error(data.error);$('#status-output').textContent=data.output}catch(error){$('#status-output').textContent=error.message}}); $('#close-dialog').addEventListener('click',()=>$('#status-dialog').close());
function escapeHTML(value){return String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]))}
