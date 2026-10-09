const log=[];const ok=(c,m)=>log.push((c?"PASS ":"FAIL ")+m);
const f=document.getElementById("f");
let phase=0;
f.onload=function(){const w=f.contentWindow,d=f.contentDocument;
 setTimeout(()=>{
  if(phase===0){
   try{w.localStorage.clear()}catch(e){}
   phase=1; w.location.reload(); return;
  }
  if(phase===1){
   ok(d.querySelectorAll('.item').length===4,'4 targets listed');
   const ta=d.getElementById('f-body'); ta.value=ta.value.replace('Congrats','Big congrats'); ta.dispatchEvent(new Event('input'));
   ok(d.getElementById('act').href.includes('Big%20congrats'),'Gmail link uses edited text');
   ok(d.getElementById('act').href.includes('Demo%20Plaza'),'Gmail link includes legal footer');
   ok(d.getElementById('edited').classList.contains('show'),'edited indicator shows');
   d.getElementById('sent').click();
   ok(d.getElementById('progtxt').textContent==='1 of 4 ready sent','progress updates');
   ok(d.getElementById('ledger').textContent.includes('--mark-sent')&&d.getElementById('ledger').textContent.includes('Northfield Builders'),'ledger command lists sent target');
   d.querySelector('[data-f="sent"]').click();
   ok(d.querySelectorAll('.item').length===1,'Sent filter shows 1');
   d.querySelector('[data-f="all"]').click();
   d.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown'}));
   ok(d.querySelector('.head h1').textContent==='Harbor & Lane Contractors','arrow key moves selection');
   d.querySelector('.tabs [data-t="followup"]').click();
   ok(d.getElementById('act').href.includes('su=Re%3A'),'follow-up opens as a reply');
   const q=d.getElementById('q'); q.value='redstone'; q.dispatchEvent(new Event('input'));
   ok(d.querySelectorAll('.item').length===1,'search filters list');
   phase=2; w.location.reload(); return;
  }
  if(phase===2){
   ok(d.getElementById('progtxt').textContent==='1 of 4 ready sent','sent state persists across reload');
   d.querySelector('.item[data-i="0"]').click();
   ok(d.getElementById('f-body').value.includes('Big congrats'),'edit persists across reload');
   d.getElementById('reset').click();
   ok(!d.getElementById('f-body').value.includes('Big congrats'),'reset restores reviewed draft');
   ok(!d.body.innerText.includes('undefined')&&!d.body.innerText.includes('NaN'),'no undefined/NaN rendered');
   document.getElementById('out').textContent=log.join('\n'); document.title='DONE';
  }
 },300);};
