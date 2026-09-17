(() => {
 'use strict';
 const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
 const form = $('#workout-form'), fields = form.elements;
 const nameOrder=new Intl.Collator('ko',{numeric:true,sensitivity:'base'});
 function selectedExercise(){const option=fields.exercise.selectedOptions[0];return {name:option?.dataset.name||'',group:option?.dataset.group||fields.category.value};}
 const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const localKey = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
 const fromKey = key => { const [y,m,d] = key.split('-').map(Number); return new Date(y,m-1,d,12); };
 const currentDate = () => { const d = new Date(); d.setHours(12,0,0,0); return d; };
 const sunday = d => { const x = new Date(d); x.setDate(x.getDate() - x.getDay()); return x; };
 const dateTitle = d => `${d.getMonth()+1}월 ${d.getDate()}일 ${['일','월','화','수','목','금','토'][d.getDay()]}요일`;
 const shifted = (d,n) => { const x = new Date(d); x.setDate(x.getDate()+n); return x; };
 let state = {version:1,days:{},library:{상체:[],하체:[],기타:[]}}, storageBlocked = false;
 function storageError(message) { $('#storage-error').textContent=message; $('#storage-error').hidden=false; }
 try {
  const raw = window.Native ? window.Native.load() : localStorage.getItem('time-fitness-v1');
  if (raw) {
   state=TimeFitnessData.normalize(JSON.parse(raw));
  }
 } catch(e) { storageBlocked=true; storageError('저장된 기록을 읽지 못했습니다. 원본은 유지됩니다. 설정에서 백업을 가져올 수 있습니다.'); }
 function persist() {
  if(storageBlocked) { toast('저장할 수 없습니다. 앱을 다시 실행해 주세요.'); return false; }
  try {
   const json=JSON.stringify(state);
   if(window.Native) { if(!window.Native.save(json)) throw Error('Native save failed'); }
   else localStorage.setItem('time-fitness-v1',json);
   $('#storage-error').hidden=true;
   return true;
  } catch(e) { storageError('기록을 저장하지 못했습니다. 저장 공간을 확인하고 다시 저장해 주세요.'); toast('저장 실패 · 화면의 내용을 유지합니다'); return false; }
 }
 let mode='week', anchor=currentDate(), selected=localKey(anchor), draft=[], editIndex=-1, baseline='', returnFocus=null, viewedBeforeEdit=false, memoDraft=[], renameFrom=null;
 let toastTimer=0;
 function toast(message) { clearTimeout(toastTimer); $('#toast').textContent=message; $('#toast').hidden=false; toastTimer=setTimeout(()=>$('#toast').hidden=true,1800); }
 function renderCalendar() {
  const todayKey=localKey(currentDate()), first=mode==='week'?sunday(anchor):new Date(anchor.getFullYear(),anchor.getMonth(),1,12);
  const count=mode==='week'?7:new Date(anchor.getFullYear(),anchor.getMonth()+1,0).getDate();
  $('#year-month').textContent=mode==='week'?`${first.getFullYear()}년`:`${anchor.getFullYear()}년`;
  const last=shifted(first,6);
  $('#period-title').textContent=mode==='week'?`${first.getMonth()+1}. ${first.getDate()} — ${last.getMonth()+1}. ${last.getDate()}`:`${anchor.getMonth()+1}월의 운동`;
  $$('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.mode===mode));
  const calendar=$('#calendar'); calendar.className=mode;
  let html=mode==='month'?['일','월','화','수','목','금','토'].map(s=>`<div class="weekday">${s}</div>`).join('')+'<div aria-hidden="true"></div>'.repeat(first.getDay()):'';
  for(let i=0;i<count;i++) {
   const date=shifted(first,i), key=localKey(date), record=state.days[key], exercises=record?.exercises||[], dow=['일','월','화','수','목','금','토'][date.getDay()];
   html+=`<button class="day ${key===todayKey?'today':''}" data-day="${key}" aria-label="${date.getMonth()+1}월 ${date.getDate()}일 ${dow}요일, 운동 ${exercises.length}개, 탭하여 조회" ${key===todayKey?'aria-current="date"':''}><span class="date"><b>${date.getDate()}</b>${mode==='week'?dow:''}${key===todayKey?'<em>오늘</em>':''}</span>`;
   if(exercises.length) {
    if(mode==='week') html+=`<span class="badge ${record.kind}">${record.kind==='pt'?'PT':'개인운동'}</span><span class="exercise-names">${exercises.slice(0,5).map(x=>`<span class="exercise-name">${escape(x.name)}</span>`).join('')}${exercises.length>5?`<span class="more">+${exercises.length-5}개 더</span>`:''}</span>`;
    else html+=`<span class="month-count ${record.kind}">${record.kind==='pt'?'PT':'개인'}<br>${exercises.length}개</span>`;
   } else if(mode==='week') html+='<span class="empty">+ 운동 등록</span>';
   html+='</button>';
  }
  calendar.innerHTML=html;
 }
 function showViewer(key,trigger) {
  selected=key; returnFocus=trigger||returnFocus;
  const date=fromKey(key), record=state.days[key], exercises=record?.exercises||[];
  $('#viewer-title').textContent=dateTitle(date);
  $('#viewer-kind').innerHTML=exercises.length?`<span class="badge ${record.kind}">${record.kind==='pt'?'PT':'개인운동'}</span>`:'';
  $('#workout-list').innerHTML=exercises.length?exercises.map((x,i)=>`<article class="workout"><div class="workout-head"><b>${escape(x.name)}</b><span>${x.kg} kg · ${x.reps}회 × ${x.sets}세트</span></div><button class="memo-toggle" data-has-memo="${Boolean(x.memo.trim())}" data-toggle-memo="${i}" aria-expanded="false" aria-controls="memo-panel-${i}" aria-label="${escape(x.name)} 메모 펼치기">◀</button><label id="memo-panel-${i}" class="memo-label" hidden><textarea data-memo="${i}" aria-label="${escape(x.name)} 메모" rows="2" maxlength="2000" placeholder="이 운동의 메모를 입력하세요">${escape(x.memo)}</textarea></label></article>`).join(''):'<p class="helper">등록한 운동이 없습니다.</p>';
  memoDraft=exercises.map(x=>x.memo);$('#memo-state').textContent='';$('#memo-save').hidden=!exercises.length;
  $('#viewer').hidden=false;
  $('[data-close="viewer"]').focus({preventScroll:true});
 }
 function memoDirty(){return memoDraft.some((m,i)=>m!==state.days[selected]?.exercises[i]?.memo);}
 function closeViewer() { if(memoDirty()&&!confirm('저장하지 않은 메모를 취소할까요?'))return; $('#viewer').hidden=true; returnFocus?.isConnected && returnFocus.focus({preventScroll:true}); }
 function syncCategory(choose='',chooseGroup=fields.category.value) {
  const all=fields.category.value==='전체 보기';
  const choices=Object.entries(state.library).filter(([group])=>all||group===fields.category.value).flatMap(([group,names])=>names.map(name=>({group,name})));
  if(choose&&!choices.some(x=>x.name===choose&&x.group===chooseGroup))choices.push({name:choose,group:chooseGroup});
  choices.sort((a,b)=>nameOrder.compare(a.name,b.name)||nameOrder.compare(a.group,b.group));
  fields.exercise.innerHTML='<option value="">'+(choices.length?'운동을 선택하세요':'등록된 운동 없음')+'</option>'+choices.map(x=>{const duplicate=choices.filter(y=>y.name===x.name).length>1;return `<option value="${escape(all?JSON.stringify([x.group,x.name]):x.name)}" data-name="${escape(x.name)}" data-group="${escape(x.group)}">${escape(x.name)}${duplicate?' · '+escape(x.group):''}</option>`;}).join('');
  if(choose){const option=[...fields.exercise.options].find(o=>o.dataset.name===choose&&o.dataset.group===chooseGroup);if(option)option.selected=true;}
  const item=selectedExercise();$('#delete-name').disabled=!(state.library[item.group]||[]).includes(item.name);$('#rename-name').disabled=!item.name;$('#name-panel').hidden=true;fields.newName.value='';$('#name-error').textContent='';renameFrom=null;
 }
 function kindColor(){fields.kind.className=fields.kind.value==='pt'?'kind-pt':'kind-solo';}
 function pendingEntry(){return fields.exercise.value!=='';}
 function resetEntry() {
  editIndex=-1; fields.exercise.value='';
  $('#add').textContent='＋ 운동 추가'; $('#cancel-entry').hidden=true; $('#entry-error').textContent=''; syncCategory();
 }
 function renderDraft() {
  $('#draft-count').textContent=draft.length;
  $('#draft-list').innerHTML=draft.length?draft.map((x,i)=>`<div class="draft-row"><button class="edit-item" type="button" data-edit="${i}"><b>${escape(x.name)}</b><span>${x.kg} kg · ${x.reps}회 × ${x.sets}세트　수정 ›</span></button><button class="remove" type="button" data-remove="${i}" aria-label="${escape(x.name)} 삭제">×</button></div>`).join(''):'<p class="helper">등록한 운동이 없습니다.</p>';
 }
 function signature() { return JSON.stringify({kind:fields.kind.value,exercises:draft}); }
 function fillEntry(index) {
  editIndex=index; const x=draft[index]; fields.category.value=Object.hasOwn(state.library,x.group)?x.group:(Object.keys(state.library).find(g=>state.library[g].includes(x.name))||'기타');syncCategory(x.name);
  fields.kg.value=x.kg; fields.reps.value=x.reps; fields.sets.value=x.sets;
  $('#add').textContent='✓ 수정 적용'; $('#cancel-entry').hidden=false; $('#entry-error').textContent=''; fields.exercise.focus({preventScroll:true});
 }
 function showEditor(key,index,trigger) {
  if(storageBlocked) { toast('저장된 기록을 읽을 수 없어 편집을 열지 못했습니다.'); return; }
  if(!$('#viewer').hidden&&memoDirty()&&!confirm('저장하지 않은 메모를 취소하고 편집할까요?'))return;
  selected=key; viewedBeforeEdit=!$('#viewer').hidden; if(trigger) returnFocus=trigger;
  const record=state.days[key]||{kind:'solo',exercises:[]}; draft=record.exercises.map(x=>({...x}));
  fields.kind.value=record.kind;fields.category.value='전체 보기';kindColor();resetEntry(); fields.kg.value=20; fields.reps.value=12; fields.sets.value=3;
  baseline=signature(); renderDraft(); const date=fromKey(key); $('#editor-title').textContent=dateTitle(date);
  $('#viewer').hidden=true; $('#editor').hidden=false;
  if(index!==undefined && draft[index]) fillEntry(index); else $('[data-close="editor"]').focus({preventScroll:true});
 }
 function closeEditor(force=false) {
  const dirty=signature()!==baseline || pendingEntry() || fields.newName.value.trim()!=='';
  if(!force && dirty && !confirm('저장하지 않은 운동 변경사항을 취소할까요?')) return false;
  $('#editor').hidden=true;
  if(viewedBeforeEdit) showViewer(selected); else returnFocus?.isConnected && returnFocus.focus({preventScroll:true});
  return true;
 }
 function addExercise() {
  const chosen=selectedExercise(),name=chosen.name.trim(), kg=Number(fields.kg.value),reps=Number(fields.reps.value),sets=Number(fields.sets.value);
  if(!name || fields.kg.value==='' || !Number.isFinite(kg) || kg<0 || !Number.isInteger(reps) || reps<1 || !Number.isInteger(sets) || sets<1) {
   $('#entry-error').textContent='운동명, 무게(0 이상), 횟수와 세트(1 이상의 정수)를 입력하세요.'; return;
  }
  const item={id:editIndex>=0?draft[editIndex].id:`${Date.now()}-${Math.random().toString(36).slice(2,8)}`,name,kg,reps,sets,group:chosen.group,memo:editIndex>=0?draft[editIndex].memo:''};
  if(editIndex>=0) draft[editIndex]=item; else draft.push(item);
  resetEntry(); renderDraft(); toast('운동 목록에 적용했습니다');
 }
 fields.category.addEventListener('change',()=>syncCategory());
 fields.kind.addEventListener('change',kindColor);
 fields.exercise.addEventListener('change',()=>{const item=selectedExercise();$('#delete-name').disabled=!(state.library[item.group]||[]).includes(item.name);$('#rename-name').disabled=!item.name;$('#name-panel').hidden=true;fields.newName.value='';renameFrom=null;});
 $('#new-name').addEventListener('click',()=>{renameFrom=null;fields.newName.value='';$('#name-error').textContent='';const all=fields.category.value==='전체 보기';$('#name-group-label').hidden=!all;$('#target-group').textContent=all?'':fields.category.value;$('#name-panel').hidden=false;fields.newName.focus();});
 $('#rename-name').addEventListener('click',()=>{const item=selectedExercise();if(!item.name)return;renameFrom=item.name;fields.newName.value=renameFrom;$('#name-group-label').hidden=true;$('#name-error').textContent='';$('#target-group').textContent=item.group;$('#name-panel').hidden=false;fields.newName.focus();fields.newName.select();});
 $('#name-cancel').addEventListener('click',()=>{$('#name-panel').hidden=true;fields.newName.value='';renameFrom=null;});
 $('#name-save').addEventListener('click',()=>{
  const group=renameFrom!==null?selectedExercise().group:(fields.category.value==='전체 보기'?fields.nameGroup.value:fields.category.value),name=fields.newName.value.trim(),list=state.library[group];
  if(!name){$('#name-error').textContent='운동명을 입력하세요.';return;}
  if(list.includes(name)&&name!==renameFrom){$('#name-error').textContent='이미 등록된 운동명입니다.';return;}
  const before=JSON.parse(JSON.stringify(state)),oldName=renameFrom;
  const matches=x=>x.name===oldName&&(x.group===group||(!Object.hasOwn(state.library,x.group)&&group==='기타'));
  if(oldName!==null){
   const index=list.indexOf(oldName);if(index>=0)list[index]=name;
   for(const day of Object.values(state.days))for(const x of day.exercises)if(matches(x))x.name=name;
  }else list.push(name);
  if(!persist()){state=before;return;}
  if(oldName!==null){for(const x of draft)if(matches(x))x.name=name;const savedBaseline=JSON.parse(baseline);for(const x of savedBaseline.exercises)if(matches(x))x.name=name;baseline=JSON.stringify(savedBaseline);renderDraft();renderCalendar();}
  syncCategory(name,group);toast('운동명을 저장했습니다');
 });
 $('#delete-name').addEventListener('click',()=>{
  const {group,name}=selectedExercise(),list=state.library[group],index=list?list.indexOf(name):-1;
  if(index<0||!confirm('운동 목록에서 삭제할까요? 캘린더에 저장한 기록은 유지됩니다.'))return;
  list.splice(index,1);if(!persist()){list.splice(index,0,name);return;}syncCategory();
 });
 syncCategory();kindColor();
 $('#add').addEventListener('click',addExercise); $('#cancel-entry').addEventListener('click',resetEntry);
 form.addEventListener('submit',e=>{
  e.preventDefault();
  if(pendingEntry()) { $('#entry-error').textContent='입력 중인 운동을 추가하거나 수정 적용해 주세요.'; $('#entry-error').scrollIntoView({block:'nearest'}); return; }
  const previous=state.days[selected];
  if(draft.length) state.days[selected]={kind:fields.kind.value,exercises:draft.map(x=>({...x}))}; else delete state.days[selected];
  if(!persist()) { if(previous) state.days[selected]=previous; else delete state.days[selected]; return; }
  baseline=signature(); closeEditor(true); renderCalendar(); toast('운동을 저장했습니다');
 });
 $('#workout-list').addEventListener('input',e=>{
  if(e.target.dataset.memo===undefined)return;memoDraft[Number(e.target.dataset.memo)]=e.target.value;document.querySelectorAll('[data-toggle-memo]')[Number(e.target.dataset.memo)].setAttribute('data-has-memo',String(Boolean(e.target.value.trim())));$('#memo-state').textContent='저장 전';
 });
 $('#workout-list').addEventListener('click',e=>{const button=e.target.closest('[data-toggle-memo]');if(!button)return;const panel=document.getElementById(button.getAttribute('aria-controls'));panel.hidden=!panel.hidden;button.textContent=panel.hidden?'◀':'▼';button.setAttribute('aria-expanded',String(!panel.hidden));const item=state.days[selected]?.exercises[Number(button.dataset.toggleMemo)];button.setAttribute('aria-label',`${item?.name||''} 메모 ${panel.hidden?'펼치기':'접기'}`);});
 $('#memo-save').addEventListener('click',()=>{
  const record=state.days[selected];if(!record)return;const before=record.exercises.map(x=>x.memo);
  record.exercises.forEach((x,i)=>x.memo=memoDraft[i]||'');
  if(!persist()){record.exercises.forEach((x,i)=>x.memo=before[i]);$('#memo-state').textContent='저장 실패';return;}
  $('#memo-state').textContent='저장됨';
 });
 document.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.day) showViewer(b.dataset.day,b);
  if(b.id==='unlock')showEditor(selected,undefined,b);
  if(b.dataset.mode) { mode=b.dataset.mode; renderCalendar(); $('#calendar-scroll').scrollTop=0; }
  if(b.dataset.nav) {
   if(mode==='week') anchor=shifted(anchor,Number(b.dataset.nav)*7);
   else anchor=new Date(anchor.getFullYear(),anchor.getMonth()+Number(b.dataset.nav),1,12);
   renderCalendar(); $('#calendar-scroll').scrollTop=0;
  }
  if(b.id==='today') { anchor=currentDate(); renderCalendar(); $('.day.today')?.scrollIntoView({block:'nearest'}); }
  if(b.dataset.close==='viewer') closeViewer(); if(b.dataset.close==='editor') closeEditor();
  if(b.dataset.edit!==undefined) { fillEntry(Number(b.dataset.edit)); fields.exercise.scrollIntoView({block:'center'}); }
  if(b.dataset.remove!==undefined) { const i=Number(b.dataset.remove); if(confirm(`${draft[i].name}을(를) 삭제할까요?`)) {draft.splice(i,1);resetEntry();renderDraft();} }
 });
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)renderCalendar();});
 document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){e.preventDefault();window.onNativeBack();return;}
  if(e.key==='Tab') {
   const modal=!$('#settings').hidden?$('#settings'):!$('#editor').hidden?$('#editor'):!$('#viewer').hidden?$('#viewer'):null;
   if(!modal)return; const items=[...modal.querySelectorAll('button,input,select,textarea,summary')].filter(x=>!x.disabled&&x.getClientRects().length);
   const first=items[0],last=items[items.length-1];
   if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();} else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }
 });
 window.onNativeBack=()=>{if(!$('#settings').hidden){$('#settings').hidden=true;incoming=null;$('#import-review').hidden=true;return true;}if(!$('#splash').hidden){$('#splash').hidden=true;return true;}if(!$('#editor').hidden){closeEditor();return true;}if(!$('#viewer').hidden){closeViewer();return true;}return false;};
 let incoming=null,fileBusy=false;
 function fileStatus(message){$('#backup-status').textContent=message;}
 function setFileBusy(value){fileBusy=value;$('#export-backup').disabled=value;$('#import-backup').disabled=value;}
 function readImport(text){
  incoming=null;$('#import-review').hidden=true;
  try{incoming=TimeFitnessData.parseBackup(text);$('#import-summary').textContent=`기록 ${incoming.days}일 · 운동 ${incoming.exercises}개 · 운동명 ${incoming.names}개`;$('#import-review').hidden=false;fileStatus('');}
  catch(e){fileStatus(e.message);}
 }
 $('#settings-open').addEventListener('click',()=>{$('#settings').hidden=false;fileStatus('');$('#settings-close').focus();});
 function closeSettings(){$('#settings').hidden=true;incoming=null;$('#import-review').hidden=true;$('#settings-open').focus();}
 $('#settings-close').addEventListener('click',closeSettings);
 for(const layer of $$('.overlay')){
  let beganOutside=false;
  layer.addEventListener('pointerdown',e=>beganOutside=e.target===layer);
  layer.addEventListener('pointercancel',()=>beganOutside=false);
  layer.addEventListener('click',e=>{
   if(e.target!==layer||!beganOutside)return;beganOutside=false;
   if(layer.id==='viewer')closeViewer();else if(layer.id==='editor')closeEditor();else if(layer.id==='settings')closeSettings();
  });
 }
 $('#export-backup').addEventListener('click',()=>{
  if(fileBusy)return;
  try{
   if(storageBlocked)throw Error('저장된 원본을 읽지 못했습니다. 먼저 정상 백업을 가져와 주세요.');
   const content=TimeFitnessData.backup(state),blob=new Blob([content],{type:'application/json'});
   if(blob.size>32*1024*1024)throw Error('백업 파일이 32MB를 초과합니다.');
   if(window.Native?.exportBackup){setFileBusy(true);fileStatus('저장 위치를 선택하세요.');if(!window.Native.exportBackup(content)){setFileBusy(false);throw Error('백업을 시작하지 못했습니다.');}}
   else{const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`TimeFitness-backup-${localKey(currentDate())}.json`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);fileStatus('백업 파일 저장을 요청했습니다.');}
  }catch(e){fileStatus(e.message);}
 });
 $('#import-backup').addEventListener('click',()=>{
  if(fileBusy)return;incoming=null;$('#import-review').hidden=true;
  if(window.Native?.importBackup){setFileBusy(true);fileStatus('백업 파일을 선택하세요.');try{window.Native.importBackup();}catch(e){setFileBusy(false);fileStatus('파일 선택 화면을 열지 못했습니다.');}}
  else $('#import-file').click();
 });
 $('#import-file').addEventListener('change',async e=>{
  const file=e.target.files[0];e.target.value='';if(!file)return;
  incoming=null;$('#import-review').hidden=true;
  try{if(file.size>32*1024*1024)throw Error('32MB 이하의 백업 파일을 선택하세요.');readImport(await file.text());}catch(error){fileStatus(error.message);}
 });
 $('#import-cancel').addEventListener('click',()=>{incoming=null;$('#import-review').hidden=true;});
 $('#import-apply').addEventListener('click',()=>{
  if(!incoming)return;
  const before=state,blocked=storageBlocked;state=incoming.data;storageBlocked=false;
  if(!persist()){state=before;storageBlocked=blocked;fileStatus('가져오지 못했습니다. 기존 데이터는 유지됩니다.');return;}
  incoming=null;$('#import-review').hidden=true;$('#viewer').hidden=true;$('#editor').hidden=true;draft=[];memoDraft=[];syncCategory();renderCalendar();fileStatus('가져왔습니다.');
 });
 if(window.Native?.takeFileResult){
  const pollFileResult=()=>{try{const raw=window.Native.takeFileResult();if(!raw)return;const result=JSON.parse(raw);setFileBusy(false);$('#settings').hidden=false;
   if(result.kind==='import')readImport(result.content);
   else if(result.kind==='export')fileStatus('백업 파일을 저장했습니다.');
   else if(result.kind==='cancel')fileStatus('취소했습니다.');
   else fileStatus(result.content||'파일 작업에 실패했습니다.');
  }catch(e){setFileBusy(false);fileStatus('파일 작업 결과를 읽지 못했습니다.');}};
  pollFileResult();setInterval(pollFileResult,500);
 }
 renderCalendar();setTimeout(()=>$('#splash').hidden=true,1200);
})();

