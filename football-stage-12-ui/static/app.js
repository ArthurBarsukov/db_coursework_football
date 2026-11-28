"use strict";
const form = document.querySelector('#filters');
const rows = document.querySelector('#players');
const statusText = document.querySelector('#status');
const previous = document.querySelector('#prev');
const next = document.querySelector('#next');
const retry = document.querySelector('#retry');
const dialog = document.querySelector('#dossier');
const detail = document.querySelector('#detail');
const sort = document.querySelector('#sort');
let offset = 0, busy = false, hasMore = false, listAbort, detailAbort;
const limit = 20;
const number = new Intl.NumberFormat('uk-UA');
const money = new Intl.NumberFormat('uk-UA', {style:'currency',currency:'EUR',maximumFractionDigits:0});
const positions = {Attack:'Нападник',Midfield:'Півзахисник',Defender:'Захисник',Goalkeeper:'Воротар'};
const feet = {left:'ліва',right:'права',both:'обидві'};
const show = value => value == null || value === '' ? '—' : String(value);
function node(tag, value, cls) { const element = document.createElement(tag); if(value !== undefined) element.textContent = show(value); if(cls) element.className=cls; return element; }
function cell(row, value) { const td=node('td',value); row.append(td); return td; }
async function request(url, signal) {
  const response = await fetch(url, {signal});
  const data = await response.json();
  if(!response.ok || data.status !== 'success') throw new Error(data.message || 'Не вдалося завантажити дані.');
  return data;
}
function buttons(){previous.disabled=busy||offset===0;next.disabled=busy||!hasMore||offset+limit>100000;}
async function load() {
  if(listAbort) listAbort.abort();
  const abort = new AbortController(); listAbort=abort;
  busy=true;hasMore=false;buttons();rows.replaceChildren();retry.hidden=true;
  statusText.className='';statusText.textContent='Завантаження гравців…';
  document.querySelector('.results').setAttribute('aria-busy','true');
  const params=new URLSearchParams();
  for(const [key,value] of new FormData(form)) if(value.trim()) params.set(key,value.trim());
  params.set('sort_by',sort.value);params.set('limit',limit);params.set('offset',offset);
  document.querySelector('#page').textContent=`Сторінка ${offset/limit+1}`;
  try {
    const result=await request(`/api/players?${params}`,abort.signal);
    for(const p of result.data){
      const tr=node('tr');const identity=cell(tr);identity.append(node('strong',p.name),node('small',`${show(p.citizenship)} · ID ${p.player_id}`));
      const club=cell(tr);club.append(node('span',p.current_club_name),node('small',positions[p.position]||p.position));
      cell(tr,p.market_value_eur==null?'—':money.format(p.market_value_eur));
      cell(tr).append(node('span',p.avg_performance_score==null?'—':p.avg_performance_score.toFixed(1),'rating'));
      const button=node('button','Досьє ↗','open');button.setAttribute('aria-label',`Відкрити досьє: ${p.name}`);button.addEventListener('click',()=>openPlayer(p.player_id));cell(tr).append(button);rows.append(tr);
    }
    hasMore=result.pagination.has_more;
    statusText.textContent=result.count?`Показано ${offset+1}–${offset+result.count} · Відкрийте досьє для перегляду виступів.`:'Гравців не знайдено. Змініть параметри або скиньте фільтри.';
  } catch(error){if(error.name==='AbortError')return;statusText.textContent=error.message;statusText.className='error';retry.hidden=false;}
  finally {if(listAbort===abort){busy=false;buttons();document.querySelector('.results').setAttribute('aria-busy','false');}}
}
async function openPlayer(id, appearanceOffset=0){
  if(detailAbort)detailAbort.abort();detailAbort=new AbortController();
  document.querySelector('#dossier-title').textContent='Досьє гравця';detail.replaceChildren(node('p','Завантаження досьє…'));if(!dialog.open)dialog.showModal();
  try{
    const {data:p, pagination}=await request(`/api/players/${id}?appearances_limit=20&appearances_offset=${appearanceOffset}`,detailAbort.signal);
    document.querySelector('#dossier-title').textContent=p.name;
    const parts=[p.citizenship,positions[p.position]||p.position,p.current_club_name,p.date_of_birth?`Дата народження: ${p.date_of_birth.slice(0,10)}`:null,p.foot?`Робоча нога: ${feet[p.foot]||p.foot}`:null,p.height_in_cm?`Зріст: ${p.height_in_cm} см`:null].filter(Boolean);
    detail.replaceChildren(node('p',parts.join(' · '),'detail-sub'));
    const metrics=node('div',undefined,'metrics');
    for(const [label,value] of [['Вартість',p.market_value_eur==null?'—':money.format(p.market_value_eur)],['Голи',number.format(p.total_goals)],['Асисти',number.format(p.total_assists)],['Рейтинг',p.avg_performance_score==null?'—':p.avg_performance_score.toFixed(1)]]){const metric=node('div',undefined,'metric');metric.append(node('strong',value),node('span',label));metrics.append(metric);}detail.append(metrics);
    if((p.warnings||[]).includes('tactical_analogs_unavailable'))detail.append(node('p','Пошук тактичних аналогів наразі недоступний. Основне досьє завантажено.','notice'));
    const historyTitle=node('h3',`Виступи · ${p.appearances.length ? appearanceOffset+1 : 0}–${appearanceOffset+p.appearances.length} із ${p.total_matches}`,'detail-title');
    detail.append(historyTitle);
    if(!p.appearances.length){detail.append(node('p','Записів про виступи немає.'));return;}
    const table=node('table');const head=node('tr');for(const label of ['Дата','Турнір','Хв.','Голи','Асисти','Рейтинг'])head.append(node('th',label));const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
    for(const a of p.appearances){const tr=node('tr');for(const value of [a.date?.slice(0,10),p.competitions[a.competition_id]?.competition_name||a.competition_id,a.minutes_played,a.goals,a.assists,a.performance_score?.toFixed(1)])cell(tr,value);body.append(tr);}table.append(body);const wrap=node('div',undefined,'table-wrap');wrap.append(table);detail.append(wrap);
    const navigation=node('nav',undefined,'pagination appearance-pagination');navigation.setAttribute('aria-label','Сторінки виступів');
    const back=node('button','← Назад','secondary');back.disabled=appearanceOffset===0;back.addEventListener('click',()=>openPlayer(id,Math.max(0,appearanceOffset-20)));
    const forward=node('button','Далі →','secondary');forward.disabled=!pagination.has_more;forward.addEventListener('click',()=>openPlayer(id,appearanceOffset+20));
    navigation.append(back,node('span',`Сторінка ${Math.floor(appearanceOffset/20)+1} із ${Math.max(1,Math.ceil(p.total_matches/20))}`),forward);
    historyTitle.after(navigation);
    if(appearanceOffset>0)historyTitle.scrollIntoView({block:'start'});
  }catch(error){if(error.name!=='AbortError')detail.replaceChildren(node('p',error.message));}
}
form.addEventListener('submit',event=>{event.preventDefault();offset=0;load();});
form.addEventListener('reset',()=>{offset=0;sort.value='market_value_eur';setTimeout(load,0);});
sort.addEventListener('change',()=>{offset=0;load();});
previous.addEventListener('click',()=>{offset=Math.max(0,offset-limit);load();});
next.addEventListener('click',()=>{offset+=limit;load();});retry.addEventListener('click',load);
document.querySelector('#close').addEventListener('click',()=>dialog.close());
dialog.addEventListener('close',()=>detailAbort?.abort());
load();
