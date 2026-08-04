const $ = (selector) => document.querySelector(selector);
const results = $('#results');
let libraryItems=[];
let currentView='discover', libraryTimer=null;
let librarySignature='';
let traktAuthTimer=null;
const toast = (message) => { const el=$('#toast'); el.textContent=message; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); };

loadTraktDashboard();

async function loadTraktDashboard(){
  const dashboard=$('#trakt-dashboard');
  try{
    const response=await fetch('/api/trakt/dashboard');
    const data=await response.json();
    if(!response.ok)throw new Error(data.error);
    dashboard.innerHTML=`${!data.configured?`<div class="trakt-setup"><strong>Connect Trakt</strong><span>${escapeHTML(data.message||'Configure Trakt to load these shelves.')}</span></div>`:''}${(data.sections||[]).map(renderTraktSection).join('')}`;
  }catch(error){dashboard.innerHTML=`<div class="trakt-setup"><strong>Trakt is unavailable</strong><span>${escapeHTML(error.message)}</span></div>`;}
}

function renderTraktSection(section){
  const items=section.items||[];
  const status=section.locked?'Connect Trakt':section.error?'Unavailable':'';
  const cards=items.length?items.map(item=>`<button class="trakt-card" data-trakt-title="${escapeHTML(item.title)}" style="${item.image?`--art:url('${escapeHTML(item.image)}')`:''}"><span class="trakt-shade"></span><span class="trakt-card-copy"><b>${escapeHTML(item.title)}</b><small>${escapeHTML([item.year,item.rating&&`★ ${Number(item.rating).toFixed(1)}`].filter(Boolean).join(' · '))}</small></span>${item.progress!=null?`<span class="trakt-progress"><i style="width:${Math.max(0,Math.min(100,Number(item.progress)||0))}%"></i></span>`:''}</button>`).join(''):`<div class="trakt-placeholder"><span>${escapeHTML(section.error||status||'Nothing here yet')}</span></div>`;
  return `<section class="trakt-row"><div class="trakt-row-head"><h2>${escapeHTML(section.title)}</h2>${status?`<span>${escapeHTML(status)}</span>`:''}</div><div class="trakt-strip">${cards}</div></section>`;
}

$('#trakt-dashboard').addEventListener('click',event=>{const card=event.target.closest('[data-trakt-title]');if(!card)return;$('#query').value=card.dataset.traktTitle;$('#search-form').requestSubmit();$('#results-title').scrollIntoView({behavior:'smooth',block:'start'});});

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

$('#discover-nav').addEventListener('click',()=>setView('discover'));
$('#library-nav').addEventListener('click',()=>setView('library'));
$('#settings-nav').addEventListener('click',()=>setView('settings'));
$('#library-filter').addEventListener('input',renderLibrary);
async function setView(view){
  currentView=view;clearTimeout(libraryTimer);
  const library=view==='library',settings=view==='settings',discover=view==='discover';
  $('#discover-nav').classList.toggle('active',discover);$('#library-nav').classList.toggle('active',library);$('#settings-nav').classList.toggle('active',settings);
  $('.hero').hidden=settings;$('#search-form').hidden=!discover;$('#trakt-dashboard').hidden=!discover;$('#library-tools').hidden=!library;$('#settings-panel').hidden=!settings;$('.bar').hidden=settings;results.hidden=settings;
  if(discover){$('#hero-eyebrow').textContent='MEDIA MANAGER · LOCAL PLAYER · PRIVATE';$('#hero-title').innerHTML='Your library.<br><em>Ready everywhere.</em>';$('#hero-lede').textContent='Everything you collect, organized and playing in seconds on every screen in your home.';$('#results-title').textContent='Ready to discover';results.innerHTML='<div class="empty"><span>⌕</span><p>Search the catalog to begin.</p></div>';return}
  if(settings){await loadTraktSettings();return}
  librarySignature='';$('#hero-eyebrow').textContent='ON THIS MAC · LIVE';$('#hero-title').innerHTML='Your collection.<br><em>Ready to play.</em>';$('#hero-lede').textContent='The files on disk, reconciled live with Unarr metadata.';$('#results-title').textContent='Loading library…';results.innerHTML='<div class="empty"><p>Reading the local library…</p></div>';await refreshLibrary();
}

async function loadTraktSettings(){
  try{const response=await fetch('/api/trakt/settings',{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error);renderTraktSettings(data);}
  catch(error){$('#trakt-account-status').textContent=error.message;}
}
function renderTraktSettings(data){
  $('#trakt-client-id').value=data.clientId||'';
  $('#trakt-client-secret').placeholder=data.hasClientSecret?'Saved — leave blank to keep it':'Enter client secret';
  const label=data.user?.name||data.user?.username;
  $('#trakt-account-title').textContent=data.authenticated?(label||'Connected to Trakt'):'Not connected';
  $('#trakt-account-status').textContent=data.authenticated?'Your personal shelves are enabled.':data.configured?'Credentials saved. Connect your Trakt account next.':'Save application credentials to begin.';
  $('#trakt-connect').disabled=!data.configured||data.authenticated;$('#trakt-connect').hidden=data.authenticated;$('#trakt-disconnect').hidden=!data.authenticated;
}
$('#trakt-settings-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;try{const response=await fetch('/api/trakt/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clientId:$('#trakt-client-id').value,clientSecret:$('#trakt-client-secret').value})});const data=await response.json();if(!response.ok)throw new Error(data.error);$('#trakt-client-secret').value='';renderTraktSettings(data);toast('Trakt credentials saved');await loadTraktDashboard();}catch(error){alert(`Could not save Trakt settings:\n\n${error.message}`)}finally{button.disabled=false}});
$('#trakt-connect').addEventListener('click',async()=>{const button=$('#trakt-connect');button.disabled=true;const authWindow=window.open('about:blank','trakt-auth');try{const response=await fetch('/api/trakt/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await response.json();if(!response.ok)throw new Error(data.error);showTraktDevice(data);if(authWindow)authWindow.location=data.verificationUrl;pollTraktAuth();}catch(error){if(authWindow)authWindow.close();alert(`Could not start Trakt authorization:\n\n${error.message}`);$('#trakt-account-status').textContent=error.message;button.disabled=false}});
function showTraktDevice(data){$('#trakt-device').hidden=false;$('#trakt-code').textContent=data.userCode;$('#trakt-activate').href=data.verificationUrl;$('#trakt-account-status').textContent='Waiting for authorization in your browser…';}
async function pollTraktAuth(){clearTimeout(traktAuthTimer);try{const response=await fetch('/api/trakt/auth',{cache:'no-store'});const data=await response.json();if(data.status==='complete'){$('#trakt-device').hidden=true;toast('Trakt connected');await loadTraktSettings();await loadTraktDashboard();return}if(data.status==='error')throw new Error(data.error);traktAuthTimer=setTimeout(pollTraktAuth,2000)}catch(error){$('#trakt-account-status').textContent=error.message;$('#trakt-connect').disabled=false;}}
$('#trakt-disconnect').addEventListener('click',async()=>{if(!confirm('Disconnect this Trakt account?'))return;const response=await fetch('/api/trakt/auth',{method:'DELETE'});const data=await response.json();if(!response.ok){alert(data.error);return}$('#trakt-device').hidden=true;await loadTraktSettings();await loadTraktDashboard();toast('Trakt disconnected');});
async function refreshLibrary(){if(currentView!=='library')return;try{const response=await fetch('/api/library',{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error);const nextItems=data.items||[];const nextSignature=JSON.stringify(nextItems);const changed=nextSignature!==librarySignature;libraryItems=nextItems;librarySignature=nextSignature;const scan=data.scan||{};const scanLabel={scheduled:'Scan scheduled',running:'Scanning metadata…',complete:'Scan complete',error:'Scan failed',idle:'Watching'}[scan.status]||'Watching';$('#transcode-state').textContent=data.transcode.available?`Live · ${scanLabel} · HLS ready`:`${scanLabel} · ffmpeg unavailable`;$('#transcode-state').title=scan.message||'';if(changed)renderLibrary();}catch(error){results.innerHTML=`<div class="empty"><p>${escapeHTML(error.message)}</p></div>`;}finally{if(currentView==='library')libraryTimer=setTimeout(refreshLibrary,3000);}}
function renderLibrary(){const term=$('#library-filter').value.trim().toLowerCase();const items=libraryItems.filter(item=>!term||`${item.title} ${item.fileName} ${item.year}`.toLowerCase().includes(term));$('#results-title').textContent=`${items.length} live library item${items.length===1?'':'s'}`;if(!items.length){results.innerHTML='<div class="empty"><p>No media files are currently present in the configured library paths.</p></div>';return}results.innerHTML=items.map(item=>{const info=item.mediaInfo||{};const video=info.video||{};const audio=info.audio||[];const subtitles=info.subtitles||[];const integrity=info.integrity||{};const duration=video.duration?`${Math.floor(video.duration/3600)}h ${Math.round(video.duration%3600/60)}m`:'';const audioOptions=audio.map((track,index)=>`<option value="${index}" ${track.default?'selected':''}>${escapeHTML(trackLabel(track,index,'Audio'))}</option>`).join('');const subtitleOptions=subtitles.map((track,index)=>`<option value="${index}">${escapeHTML(trackLabel(track,index,'Subtitle'))}</option>`).join('');return `<article class="card library-card"><div class="meta">${escapeHTML([item.year,item.quality,item.season&&`S${item.season}E${item.episode}`,!item.indexed&&'NEW'].filter(Boolean).join(' · '))}</div><h3>${escapeHTML(item.title)}</h3><p>${escapeHTML(item.fileName)}</p>${integrity.damaged?`<p class="integrity">Warning: ${escapeHTML(integrity.reason||'integrity issue')}</p>`:''}<div class="media-summary"><span>${escapeHTML(video.codec||item.codec||'unscanned')}</span>${video.width?`<span>${video.width}×${video.height}</span>`:''}${video.hdr?`<span>${escapeHTML(video.hdr)}</span>`:''}${duration?`<span>${duration}</span>`:''}<span>${formatBytes(item.fileSize)}</span></div>${audio.length||subtitles.length?`<div class="track-pickers">${audio.length?`<label>Audio<select class="library-audio">${audioOptions}</select></label>`:''}${subtitles.length?`<label>Subtitles<select class="library-subtitle"><option value="-1">Off</option>${subtitleOptions}</select></label>`:''}</div>`:''}<button class="play-library" data-library="${item.id}" data-title="${escapeHTML(item.title)}">Play with HLS</button></article>`}).join('');}

function render(items){ $('#results-title').textContent=`${items.length} result${items.length===1?'':'s'}`; if(!items.length){results.innerHTML='<div class="empty"><p>No matches found. Try a broader search.</p></div>';return}
  results.innerHTML=items.map(item=>{const torrents=item.torrents||item.releases||[]; const title=item.title||item.name||'Untitled'; const rating=item.ratingImdb||item.ratingTmdb||item.rating; return `<article class="card"><div class="meta">${escapeHTML([item.contentType||item.type,item.year,rating&&`★ ${rating}`].filter(Boolean).join(' · '))}</div><h3>${escapeHTML(title)}</h3><p>${escapeHTML(item.overview||item.description||'')}</p><div class="releases">${torrents.slice(0,3).map(release=>{const hash=release.infoHash||release.info_hash||'';return `<div class="release"><b>${escapeHTML(release.quality||release.resolution||'Release')}</b><span>${escapeHTML(String(release.seeders??''))}${release.seeders!=null?' seeds':''}</span>${hash?`<button data-download="${escapeHTML(hash)}">Download</button><button class="stream" data-stream="${escapeHTML(hash)}" data-title="${escapeHTML(title)}">Stream</button>`:''}</div>`}).join('')}</div></article>`}).join('');
}
results.addEventListener('click',async event=>{const button=event.target.closest('[data-download]');if(!button)return;button.disabled=true;button.textContent='Starting…';try{const response=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({infoHash:button.dataset.download})});const data=await response.json();if(!response.ok)throw new Error(data.error);button.textContent='Started';toast('Download started in unarr');}catch(error){button.disabled=false;button.textContent='Download';toast(error.message)}});
results.addEventListener('click',async event=>{const button=event.target.closest('[data-stream]');if(!button)return;button.disabled=true;alert('Unarr will find peers and build a playback buffer. The web player will open now and show live progress.');await launchPlayer('/api/stream',{infoHash:button.dataset.stream},button.dataset.title,button);});
results.addEventListener('click',async event=>{const button=event.target.closest('[data-library]');if(!button)return;button.disabled=true;const card=button.closest('.library-card');const audioIndex=Number(card.querySelector('.library-audio')?.value||0);const subtitleIndex=Number(card.querySelector('.library-subtitle')?.value??-1);alert('Unarr will prepare browser-compatible HLS using your selected audio, subtitles, and quality. The web player will open now.');await launchPlayer('/api/library/stream',{itemId:button.dataset.library,quality:$('#library-quality').value,audioIndex,subtitleIndex},button.dataset.title,button);});
async function launchPlayer(endpoint,body,title,button){try{const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error);location.href=`/watch.html?session=${encodeURIComponent(data.id)}&title=${encodeURIComponent(title)}`;}catch(error){button.disabled=false;alert(`Playback could not start:\n\n${error.message}`);}}
$('#status-button').addEventListener('click',async()=>{const dialog=$('#status-dialog');dialog.showModal();$('#status-output').textContent='Loading…';try{const response=await fetch('/api/status');const data=await response.json();if(!response.ok)throw new Error(data.error);$('#status-output').textContent=data.output}catch(error){$('#status-output').textContent=error.message}}); $('#close-dialog').addEventListener('click',()=>$('#status-dialog').close());
function escapeHTML(value){return String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]))}
function formatBytes(value){const units=['B','KB','MB','GB','TB'];let size=Number(value)||0,index=0;while(size>=1000&&index<units.length-1){size/=1000;index++}return `${size.toFixed(index?1:0)} ${units[index]}`}
function trackLabel(track,index,prefix){const imageSubtitles=['hdmv_pgs_subtitle','dvd_subtitle','dvb_subtitle'];const parts=[track.lang&&track.lang.toUpperCase(),track.title,track.codec,imageSubtitles.includes(track.codec)&&'burn-in',track.channels&&`${track.channels}ch`].filter(Boolean);return parts.join(' · ')||`${prefix} ${index+1}`}
