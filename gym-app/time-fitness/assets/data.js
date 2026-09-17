(function(scope){
 'use strict';
 const groups=['상체','하체','기타'];
 function normalize(raw){
  if(!raw || raw.version!==1 || !raw.days || typeof raw.days!=='object' || Array.isArray(raw.days)) throw Error('타임 피트니스 데이터가 아닙니다.');
  const result={version:1,days:{},library:{상체:[],하체:[],기타:[]}};
  for(const [key,day] of Object.entries(raw.days)){
   if(!/^\d{4}-\d{2}-\d{2}$/.test(key))throw Error('날짜 정보가 올바르지 않습니다.');
   const [y,m,d]=key.split('-').map(Number),date=new Date(y,m-1,d);
   if(y<1900||date.getFullYear()!==y||date.getMonth()!==m-1||date.getDate()!==d)throw Error('날짜 정보가 올바르지 않습니다.');
   if(!day||!['solo','pt'].includes(day.kind)||!Array.isArray(day.exercises))throw Error('운동 기록이 올바르지 않습니다.');
   result.days[key]={kind:day.kind,exercises:day.exercises.map((x,i)=>{
    if(!x||typeof x.name!=='string'||!x.name.trim()||x.name.length>200||!Number.isFinite(x.kg)||x.kg<0||!Number.isInteger(x.reps)||x.reps<1||!Number.isInteger(x.sets)||x.sets<1||typeof x.memo!=='string'||x.memo.length>10000)throw Error('운동 상세 정보가 올바르지 않습니다.');
    return {id:typeof x.id==='string'?x.id:`${key}-${i}`,name:x.name,kg:x.kg,reps:x.reps,sets:x.sets,memo:x.memo,group:groups.includes(x.group)?x.group:'기타'};
   })};
  }
  if(raw.library!==undefined){
   if(!raw.library||typeof raw.library!=='object'||Array.isArray(raw.library))throw Error('운동 목록이 올바르지 않습니다.');
   for(const group of groups){const list=raw.library[group];if(!Array.isArray(list)||list.some(n=>typeof n!=='string'||!n.trim()||n.length>200))throw Error('운동 목록이 올바르지 않습니다.');result.library[group]=[...new Set(list)];}
  }
  return result;
 }
 function parseBackup(text){
  let raw;try{raw=JSON.parse(text.replace(/^\uFEFF/,''));}catch(e){throw Error('백업 파일을 읽을 수 없습니다.');}
  if(raw?.format!==undefined){if(raw.format!=='time-fitness-backup'||raw.version!==1)throw Error('지원하지 않는 백업 형식 또는 버전입니다.');raw=raw.data;}
  const data=normalize(raw);
  return {data,days:Object.keys(data.days).length,exercises:Object.values(data.days).reduce((a,d)=>a+d.exercises.length,0),names:Object.values(data.library).reduce((a,n)=>a+n.length,0)};
 }
 function backup(state){return JSON.stringify({format:'time-fitness-backup',version:1,exportedAt:new Date().toISOString(),data:normalize(state)},null,2);}
 const api={normalize,parseBackup,backup};scope.TimeFitnessData=api;if(typeof module!=='undefined')module.exports=api;
})(typeof window==='undefined'?globalThis:window);
